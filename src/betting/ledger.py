"""
Persistent P&L ledger for live bets.

CSV at results/ledger.csv — one row per bet, human-editable in Excel.
Settlement is automatic via martj42 results CSV, with TheOddsAPI as same-day
primary source for WM 2026 matches.
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("sportsbrain.betting.ledger")

import json
from datetime import date

import pandas as pd

from src.betting.value_detector import BetSignal
from src.config import (
    BANKROLL_SNAPSHOT_PATH,
    BANKROLL_START,
    DEFAULT_USER,
    RESULTS_DIR,
    bankroll_snapshot_path_for,
    ledger_path_for,
)
from src.utils.atomic_io import atomic_write_json

# Legacy single-user ledger path (used as the migration source).
_LEGACY_LEDGER_PATH = RESULTS_DIR / "ledger.csv"


def _resolve_ledger_path(ledger_path: Path | None = None, user: str = DEFAULT_USER) -> Path:
    """Resolves the per-user ledger path. Migrates the legacy `ledger.csv`
    into the default user's slot on first call (one-shot rename).

    Sentinel paths (`None`, the legacy `ledger.csv`, or the resolved default
    user path) trigger per-user resolution + migration. Explicit other paths
    (tests/manual overrides) are returned unchanged."""
    sentinels = {None, _LEGACY_LEDGER_PATH, ledger_path_for(DEFAULT_USER)}
    if ledger_path not in sentinels:
        return ledger_path
    user_path = ledger_path_for(user)
    if (not user_path.exists()
            and user == DEFAULT_USER
            and _LEGACY_LEDGER_PATH.exists()):
        user_path.parent.mkdir(parents=True, exist_ok=True)
        _LEGACY_LEDGER_PATH.rename(user_path)
    return user_path


# Public alias — resolved at import to the default user's per-user file.
# P0D-002: wrapped in try/except so importing this module does NOT fail when
# SPORTSBRAIN_LEDGER_DIR is not set (e.g. during CI module-import smoke tests).
# Functions that use LEDGER_PATH must call ledger_path_for() themselves; the
# None sentinel will raise EnvironmentError at call-time, not import-time.
try:
    LEDGER_PATH = _resolve_ledger_path(None, DEFAULT_USER)
except EnvironmentError:
    LEDGER_PATH = None  # Will fail properly when actually used via ledger_path_for()


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
        url = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup_2026/scores/"
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
    except Exception as exc:
        _log.debug("WM scores fetch failed: %s", exc)
        return {}

_FIELDS = [
    "match_id", "match_date", "home", "away", "market",
    "decimal_odds", "stake_pct", "stake_amount",
    "placed_date", "status", "pnl", "closing_odds", "clv",
    "pinnacle_ref_odds",
    "source", "model_prob", "stake_reason",
    "league",  # Registry short-code (wm2026 | bl2 | atp | wta | ...) — settle_from_results routet danach
    # P0-A: canonical bet identity + risk provenance (new rows only; legacy rows get "")
    "signal_id", "fixture_key", "sport",
    "bankroll_at_placement", "cap_applied",
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
    if "stake_reason" not in df.columns:
        df["stake_reason"] = ""
    if "league" not in df.columns:
        # Truly legacy rows (pre-league column): historically all WM 2026 football.
        df["league"] = "wm2026"
    else:
        df["league"] = df["league"].fillna("")
        # P0-A: Tennis rows must NEVER default to wm2026.
        # For blank league, apply wm2026 only if sport is not tennis.
        sport_col = df["sport"] if "sport" in df.columns else pd.Series("", index=df.index)
        is_tennis = sport_col.str.strip().str.lower() == "tennis"
        blank_league = df["league"].eq("")
        # Football/unknown blank rows → wm2026 (backward-compat)
        df.loc[blank_league & ~is_tennis, "league"] = "wm2026"
        # Tennis blank-league rows stay blank — UNKNOWN, never wm2026
    # P0-A: canonical identity fields — empty string for legacy rows
    for _col in ("signal_id", "fixture_key", "sport", "bankroll_at_placement", "cap_applied"):
        if _col not in df.columns:
            df[_col] = ""
        else:
            df[_col] = df[_col].fillna("")
    return df


def _save(df: pd.DataFrame, path: Path) -> None:
    # Atomic write: partial CSVs bei Crash während to_csv() haben in der Vergangenheit
    # Ledger corrupted. tempfile + os.replace() vermeidet das.
    import os as _os
    import tempfile as _tf
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = _tf.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    _os.close(fd)
    try:
        df.to_csv(tmp_name, index=False)
        _os.replace(tmp_name, path)
    except Exception:
        try:
            _os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # Sync to SQLite — keeps open_bets() consistent with CSV (N-Rev5 write-path fix)
    try:
        from src.betting.db import open_db
        db_path = path.with_suffix(".db")
        conn = open_db(db_path)
        try:
            for _, row in df.iterrows():
                r = dict(row)
                conn.execute("""
                    INSERT OR REPLACE INTO bets
                    (match_id, match_date, home, away, market, decimal_odds, stake_pct,
                     stake_amount, placed_date, status, pnl, closing_odds, clv,
                     pinnacle_ref_odds, source, model_prob, stake_reason,
                     league, signal_id, fixture_key, sport, bankroll_at_placement, cap_applied)
                    VALUES (:match_id,:match_date,:home,:away,:market,:decimal_odds,:stake_pct,
                            :stake_amount,:placed_date,:status,:pnl,:closing_odds,:clv,
                            :pinnacle_ref_odds,:source,:model_prob,:stake_reason,
                            :league,:signal_id,:fixture_key,:sport,:bankroll_at_placement,:cap_applied)
                """, {
                    "match_id": r.get("match_id",""),
                    "match_date": r.get("match_date",""),
                    "home": r.get("home",""), "away": r.get("away",""),
                    "market": r.get("market",""),
                    "decimal_odds": _to_float(r.get("decimal_odds")),
                    "stake_pct": _to_float(r.get("stake_pct")),
                    "stake_amount": _to_float(r.get("stake_amount")),
                    "placed_date": r.get("placed_date",""),
                    "status": r.get("status","open"),
                    "pnl": _to_float(r.get("pnl")) or 0.0,
                    "closing_odds": _to_float(r.get("closing_odds")),
                    "clv": _to_float(r.get("clv")),
                    "pinnacle_ref_odds": _to_float(r.get("pinnacle_ref_odds")),
                    "source": r.get("source","value") or "value",
                    "model_prob": _to_float(r.get("model_prob")),
                    "stake_reason": r.get("stake_reason","") or "",
                    # P0-A: canonical identity + risk provenance
                    "league":                r.get("league","") or "",
                    "signal_id":             r.get("signal_id","") or "",
                    "fixture_key":           r.get("fixture_key","") or "",
                    "sport":                 r.get("sport","") or "",
                    "bankroll_at_placement": r.get("bankroll_at_placement","") or "",
                    "cap_applied":           r.get("cap_applied","") or "",
                })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        _log.warning("SQLite sync failed: %s", exc)


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def append_bets(
    signals: list[BetSignal],
    bankroll: float,
    path: Path | None = None,
    match_date: str = "",
    *,
    user: str = DEFAULT_USER,
) -> int:
    """
    Appends new BetSignals as 'open' to the ledger CSV.
    Skips duplicates (same match_id + market already present).
    Returns number of new rows written.
    """
    if not signals:
        return 0

    path = _resolve_ledger_path(path, user)
    with _file_lock(path):
        df = _load(path)
        existing = set(zip(df.get("match_id", pd.Series([])), df.get("market", pd.Series([]))))

        today = pd.Timestamp.now().strftime("%Y-%m-%d")
        # O1-4: Tennis market prefixes that must NEVER land under a football league.
        # Root-cause: tennis_detector._signal() didn't set BetSignal.league → "wm2026" fallback.
        _TENNIS_ONLY_PREFIXES = ("o/u_games_", "o/u_sets_", "score_", "first_set_")

        new_rows = []
        for s in signals:
            # Safety-Gate: Display-only-Signale (no_bet_flag) NIE in den Ledger.
            # Modell-implizite Preise haben keine Marktreferenz → kein CLV, kein Settlement.
            if getattr(s, "no_bet_flag", False):
                continue
            # O1-4 integrity gate: tennis-specific markets must carry atp/wta league.
            s_league = getattr(s, "league", "")
            if any(s.market.startswith(p) for p in _TENNIS_ONLY_PREFIXES) and s_league == "wm2026":
                raise ValueError(
                    f"O1-4 integrity violation: tennis market '{s.market}' has league='wm2026'. "
                    "BetSignal.league must be 'atp' or 'wta' for tennis signals."
                )
            if s_league in ("atp", "wta", "challenger_atp") and s.market == "draw":
                raise ValueError(
                    f"O1-4 integrity violation: tennis league '{s_league}' on football market 'draw'."
                )
            key = (s.match_id, s.market)
            if key in existing:
                import logging as _log
                _log.getLogger("sportsbrain.ledger").info(
                    "Skip duplicate: %s %s (bereits im Ledger)", s.match_id, s.market
                )
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
                "stake_reason":  getattr(s, "stake_reason", "") or "",
                "league":        getattr(s, "league", "") or "",
            })

        if new_rows:
            new_df = pd.DataFrame(new_rows, columns=_FIELDS)
            df = pd.concat([df, new_df], ignore_index=True)
            _save(df, path)

    return len(new_rows)


def settle_from_results(
    ledger_path: Path | None = None,
    results: pd.DataFrame | None = None,
    *,
    user: str = DEFAULT_USER,
) -> int:
    """
    Settles open bets against martj42 results.
    1X2: home/away_score determines winner.
    O/U: home_score + away_score vs 2.5.
    Returns number of newly settled bets.
    """
    ledger_path = _resolve_ledger_path(ledger_path, user)
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

    # Score-Lookup pro Liga aufbauen. Das ersetzt den ehemaligen hardcoded
    # WM-Filter. Bei explizit übergebenem `results` (Test-Injection) bleibt der
    # alte WM-Pfad aktiv — nur produktive Aufrufe (results=None) routen via
    # results_router.fetch_results_for_league().
    from src.config import canonical_name as _cn
    score_lookup: dict[tuple[str, str], tuple[int, int]] = {}

    if results is not None:
        # Backward compat: explicit results (WM-Testpfad).
        if "tournament" in results.columns:
            wm_results = results[
                (results.get("tournament", pd.Series()) == "FIFA World Cup")
                & (results["date"] >= pd.Timestamp("2026-06-11"))
                & results["home_score"].notna()
            ]
        else:
            wm_results = results.iloc[:0]
        for _, row in wm_results.iterrows():
            key = (_cn(str(row["home_team"])), _cn(str(row["away_team"])))
            try:
                score_lookup[key] = (int(row["home_score"]), int(row["away_score"]))
            except (ValueError, TypeError):
                continue
    else:
        # Route per open-bet-league via results_router. Jede Liga liefert eigenes Dict.
        from src.data.results_router import fetch_results_for_league
        leagues_in_ledger = sorted(set(df.loc[open_mask, "league"].dropna().tolist()))
        for league_short in leagues_in_ledger:
            try:
                score_lookup.update(fetch_results_for_league(league_short))
            except Exception as exc:
                _log.debug("results_router[%s] failed: %s", league_short, exc)

    # TheOddsAPI-Scores für WM (schneller Same-Day-Path). 2.BL nutzt football-data
    # + eigenen ESPN-Fallback (Phase E), hier bewusst nur WM-Scores.
    live_scores = _fetch_completed_wm_scores() if any(
        df.loc[open_mask, "league"] == "wm2026"
    ) else {}

    settled = 0
    newly_settled_indices: list = []
    for idx in df[open_mask].index:
        row = df.loc[idx]
        home, away = str(row["home"]), str(row["away"])
        market = str(row["market"])
        odds = float(row["decimal_odds"])
        stake = float(row["stake_amount"])

        # Look up score: live API first (WM only), then league-scoped result loader
        from src.config import canonical_name
        score_key = (canonical_name(home), canonical_name(away))
        if score_key in live_scores:
            hg, ag = live_scores[score_key]
        elif score_key in score_lookup:
            hg, ag = score_lookup[score_key]
        else:
            continue
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
        df = _backfill_clv_bl2(df)
        _save(df, ledger_path)
        import os
        if not os.getenv("PYTEST_CURRENT_TEST"):
            try:
                from src.notifications.web_push import send_settlement_alert
                summary = ledger_summary(ledger_path)
                for idx in newly_settled_indices:
                    send_settlement_alert(df.loc[idx].to_dict(), summary)
            except Exception as exc:
                _log.debug("settlement push failed: %s", exc)
            _refresh_dashboard(user)

    return settled


def _backfill_clv_bl2(df: pd.DataFrame) -> pd.DataFrame:
    """Füllt closing_odds + clv für BL2-Zeilen aus dem Closing-Odds-Snapshot-Cache."""
    from src.config import DATA_CACHE
    cache_p = DATA_CACHE / "bundesliga2_closing_odds.json"
    if not cache_p.exists():
        return df
    try:
        closing_cache = json.loads(cache_p.read_text())
    except Exception:
        return df

    _market_key_map = {"home": "closing_home", "draw": "closing_draw", "away": "closing_away"}
    bl2_mask = df.get("league", pd.Series(dtype=str)) == "bl2"
    for idx, row in df[bl2_mask].iterrows():
        if str(df.at[idx, "closing_odds"] or "").strip() not in ("", "0.0", "0"):
            continue  # bereits gesetzt
        date_str = str(row.get("match_date", ""))[:10].replace("-", "")
        match_key = f"{row.get('home', '')}_vs_{row.get('away', '')}_{date_str}"
        entry = closing_cache.get(match_key, {})
        market = str(row.get("market", ""))
        co = entry.get(_market_key_map.get(market, market))
        if not co:
            continue
        try:
            co_f = float(co)
            odds_f = float(row.get("decimal_odds") or 0)
            if co_f > 1.0 and odds_f > 1.0:
                df.at[idx, "closing_odds"] = f"{co_f:.3f}"
                clv = max(-0.99, min(2.00, odds_f / co_f - 1.0))
                df.at[idx, "clv"] = f"{clv:.4f}"
        except (ValueError, TypeError):
            continue
    return df


def count_open_bets(path: Path | None = None, *, user: str = DEFAULT_USER) -> int:
    """Returns number of bets with status='open'. Prefers SQLite (no CSV lock needed)."""
    path = _resolve_ledger_path(path, user)
    db_path = path.with_suffix(".db")
    if db_path.exists():
        try:
            from src.betting.db import open_db
            conn = open_db(db_path)
            try:
                row = conn.execute("SELECT COUNT(*) FROM bets WHERE status='open'").fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as exc:
            _log.debug("SQLite open_bets count failed, falling back to CSV: %s", exc)
    df = _load(path)
    if df.empty:
        return 0
    return int((df["status"] == "open").sum())


def ledger_summary(path: Path | None = None, *, user: str = DEFAULT_USER) -> dict:
    """
    Returns summary stats: n_bets, n_open, n_won, n_lost, total_staked, total_pnl, roi_pct, win_rate.
    """
    path = _resolve_ledger_path(path, user)
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


def clv_by_source(
    path: Path | None = None,
    *,
    user: str = DEFAULT_USER,
    n_min: int = 5,
) -> dict[str, dict]:
    """Returns per-source CLV stats for settled bets.

    Only sources with >= n_min CLV readings are included.
    Result: {"value": {"mean_clv": 0.02, "n": 34}, "tennis_inplay": {...}, ...}
    """
    path = _resolve_ledger_path(path, user)
    df = _load(path)
    settled = df[df["status"].isin(["won", "lost"])].copy()
    if settled.empty or "clv" not in settled.columns:
        return {}
    settled["clv_num"] = pd.to_numeric(settled.get("clv", pd.Series([])), errors="coerce")
    result: dict[str, dict] = {}
    for src, grp in settled.groupby("source"):
        vals = grp["clv_num"].dropna()
        if len(vals) < n_min:
            continue
        result[str(src)] = {"mean_clv": float(vals.mean()), "n": len(vals)}
    return result


def _current_iso_week(today: date | None = None) -> tuple[int, int]:
    d = today or date.today()
    iso = d.isocalendar()
    return iso[0], iso[1]


def _live_bankroll(ledger_path: Path | None = None, *, user: str = DEFAULT_USER) -> float:
    """BANKROLL_START + total realized P&L (excluding open bets)."""
    s = ledger_summary(ledger_path, user=user)
    return round(BANKROLL_START + s["total_pnl"], 2)


def _resolve_snapshot_path(snapshot_path: Path | None, user: str) -> Path:
    """Resolves the per-user snapshot path and migrates legacy single-user
    `bankroll_snapshot.json` into the default user's slot on first call.

    Explicit `snapshot_path` (e.g. from tests) bypasses migration and is used
    as-is."""
    if snapshot_path is not None:
        return snapshot_path
    user_path = bankroll_snapshot_path_for(user)
    if not user_path.exists() and user == DEFAULT_USER and BANKROLL_SNAPSHOT_PATH.exists():
        # one-shot migration: rename legacy file into the default user's slot.
        user_path.parent.mkdir(parents=True, exist_ok=True)
        BANKROLL_SNAPSHOT_PATH.rename(user_path)
    return user_path


def peek_bankroll_snapshot(
    snapshot_path: Path | None = None,
    ledger_path: Path | None = None,
    user: str = DEFAULT_USER,
) -> float:
    """Read-only: returns cached snapshot if valid for current ISO week,
    otherwise the live computed bankroll. Does NOT write to disk."""
    snapshot_path = _resolve_snapshot_path(snapshot_path, user)
    year, week = _current_iso_week()
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            if data.get("iso_year") == year and data.get("iso_week") == week:
                return float(data["bankroll"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    return _live_bankroll(ledger_path, user=user)


def get_bankroll_snapshot(
    snapshot_path: Path | None = None,
    ledger_path: Path | None = None,
    user: str = DEFAULT_USER,
) -> float:
    """Returns bankroll for the current ISO week. On the first call of a new
    ISO week, recomputes from the ledger and persists the snapshot. Within
    the same week, returns the cached value so stakes stay stable across
    intra-week wins/losses (drawdown smoothing — user choice over HWM)."""
    snapshot_path = _resolve_snapshot_path(snapshot_path, user)
    year, week = _current_iso_week()
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            if data.get("iso_year") == year and data.get("iso_week") == week:
                return float(data["bankroll"])
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
    bankroll = _live_bankroll(ledger_path, user=user)
    atomic_write_json(snapshot_path, {
        "iso_year": year,
        "iso_week": week,
        "snapshot_date": date.today().isoformat(),
        "bankroll": bankroll,
        "user": user,
    }, indent=2)
    return bankroll


def _refresh_dashboard(user: str = DEFAULT_USER) -> None:
    try:
        from src.notifications.web_dashboard import write_signals_json
        write_signals_json(user=user)
    except Exception:
        pass


def cancel_bet(
    home: str,
    away: str,
    market: str,
    path: Path | None = None,
    snapshot_path: Path | None = None,
    *,
    user: str = DEFAULT_USER,
) -> str:
    """Mark a bet as cancelled (status=cancelled) and refund stake to snapshot.

    Returns: "ok" | "already_cancelled" | "not_found" | "already_settled"
    Idempotent: calling twice returns "already_cancelled" without double-refund.
    """
    path = _resolve_ledger_path(path, user)
    with _file_lock(path):
        df = _load(path)
        mask = (
            (df["home"].str.strip().str.lower() == home.strip().lower()) &
            (df["away"].str.strip().str.lower() == away.strip().lower()) &
            (df["market"].str.strip() == market.strip())
        )
        matches = df[mask]
        if matches.empty:
            return "not_found"

        idx = matches.index[-1]
        current_status = df.at[idx, "status"]

        if current_status == "cancelled":
            return "already_cancelled"
        if current_status in ("won", "lost", "void"):
            return "already_settled"

        stake = float(df.at[idx, "stake_amount"] or 0)
        df.at[idx, "status"] = "cancelled"
        _save(df, path)

    # Refund stake to snapshot (adjust bankroll up by stake amount)
    snap_path = _resolve_snapshot_path(snapshot_path, user)
    if snap_path.exists() and stake > 0:
        try:
            data = json.loads(snap_path.read_text())
            data["bankroll"] = round(float(data.get("bankroll", 0)) + stake, 2)
            atomic_write_json(snap_path, data, indent=2)
        except Exception:
            pass

    import os
    if not os.getenv("PYTEST_CURRENT_TEST"):
        _refresh_dashboard(user)
    return "ok"
