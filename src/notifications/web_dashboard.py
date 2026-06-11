"""
Writes docs/data/signals.json for the GitHub Pages web dashboard.
Called at the end of daily_scan.py and tennis_scan.py.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.betting.value_detector import BetSignal

ROOT = Path(__file__).parent.parent.parent
_JSON_PATH = ROOT / "docs" / "data" / "signals.json"
_LEDGER_PATH = ROOT / "results" / "ledger.csv"


def _build_history(n_days: int = 30) -> list[dict]:
    """Read ledger CSV and return daily P&L history (most recent first)."""
    if not _LEDGER_PATH.exists():
        return []
    try:
        daily: dict[str, dict] = defaultdict(lambda: {"n_bets": 0, "staked": 0.0, "pnl": 0.0})
        with open(_LEDGER_PATH, newline="") as f:
            for row in csv.DictReader(f):
                date = (row.get("placed_date") or row.get("match_date") or "")[:10].strip()
                if not date:
                    continue
                daily[date]["n_bets"] += 1
                daily[date]["staked"] += float(row.get("stake_amount") or 0)
                daily[date]["pnl"]    += float(row.get("pnl") or 0)
        result = []
        for date in sorted(daily.keys(), reverse=True)[:n_days]:
            d = daily[date]
            roi = (d["pnl"] / d["staked"] * 100) if d["staked"] > 0 else 0.0
            result.append({
                "date":    date,
                "n_bets":  d["n_bets"],
                "pnl":     round(d["pnl"], 2),
                "roi_pct": round(roi, 1),
            })
        return result
    except Exception:
        return []


def _signal_to_dict(
    s: BetSignal,
    sport: str = "football",
    tour: str = "",
    kickoff: str = "",
) -> dict:
    d = {
        "sport":      sport,
        "match":      f"{s.home} vs {s.away}",
        "market":     s.market,
        "odds":       round(s.decimal_odds, 2),
        "model_prob": round(s.model_prob * 100, 1),
        "ev_pct":     round(s.ev * 100, 1),
        "stake_eur":  round(s.stake_eur, 2),
        "confidence": s.confidence,
    }
    if tour:
        d["tour"] = tour
    if kickoff:
        d["kickoff"] = kickoff
    return d


def _build_wm_stats() -> dict:
    """Aggregiert WM-Performance-Stats aus dem Ledger."""
    if not _LEDGER_PATH.exists():
        return {}
    try:
        stats = {
            "1x2":   {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "ou25":  {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "btts":  {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
            "other": {"n": 0, "won": 0, "staked": 0.0, "pnl": 0.0},
        }
        bankroll_series = [{"date": "2026-06-11", "balance": 100.0}]
        balance = 100.0
        with open(_LEDGER_PATH, newline="") as f:
            for row in sorted(csv.DictReader(f), key=lambda r: r.get("match_date", "")):
                status = row.get("status", "")
                if status not in ("won", "lost", "push"):
                    continue
                mkt = row.get("market", "")
                stake = float(row.get("stake_amount", 0))
                pnl = float(row.get("pnl", 0))
                date = row.get("match_date", "")[:10]
                # Marktgruppe
                if mkt in ("home", "draw", "away"):
                    grp = "1x2"
                elif "o/u2.5" in mkt or "o/u1.5" in mkt or "o/u3.5" in mkt:
                    grp = "ou25"
                elif "btts" in mkt:
                    grp = "btts"
                else:
                    grp = "other"
                stats[grp]["n"] += 1
                stats[grp]["won"] += 1 if status == "won" else 0
                stats[grp]["staked"] += stake
                stats[grp]["pnl"] += pnl
                balance += pnl
                bankroll_series.append({"date": date, "balance": round(balance, 2)})
        # Compute hit-rates
        for grp in stats:
            d = stats[grp]
            d["hit_rate"] = round(d["won"] / d["n"] * 100, 1) if d["n"] > 0 else None
            d["roi"] = round(d["pnl"] / d["staked"] * 100, 1) if d["staked"] > 0 else None
            d["staked"] = round(d["staked"], 2)
            d["pnl"] = round(d["pnl"], 2)
        return {"stats": stats, "series": bankroll_series}
    except Exception:
        return {}


def _get_closed_bets() -> list[dict]:
    if not _LEDGER_PATH.exists():
        return []
    try:
        with open(_LEDGER_PATH, newline="") as f:
            return [r for r in csv.DictReader(f) if r.get("status") in ("won", "lost", "push")]
    except Exception:
        return []


def write_signals_json(
    football: list[BetSignal] | None = None,
    tennis: list[BetSignal] | None = None,
    portfolio: dict | None = None,
    top_elo: list[tuple[str, float]] | None = None,
    tennis_tour_map: dict[str, str] | None = None,
    kickoff_map: dict[str, str] | None = None,
    schedule: list[dict] | None = None,
    all_odds: dict[str, dict] | None = None,
    model_tips: dict[str, dict] | None = None,
    open_bets: list[dict] | None = None,
    odds_history: dict | None = None,  # {match_key: [{date, home, draw, away}]}
) -> None:
    """
    Writes (or merges into) docs/data/signals.json.
    Merges football and tennis so each scanner can call independently.

    schedule: optional list of all upcoming matches (not just value bets) —
              each dict: {sport, home, away, kickoff, tour?}
    tennis_tour_map: optional {match_id: "atp"|"wta"} — adds tour field to tennis signals
    kickoff_map: optional {match_id: "ISO-8601"} — adds kickoff time to all signals
    """
    football = football or []
    tennis = tennis or []
    portfolio = portfolio or {}
    top_elo = top_elo or []
    tennis_tour_map = tennis_tour_map or {}
    kickoff_map = kickoff_map or {}

    # Load existing JSON to merge sport sections
    existing: dict = {}
    if _JSON_PATH.exists():
        try:
            existing = json.loads(_JSON_PATH.read_text())
        except Exception:
            existing = {}

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    football_data = [
        _signal_to_dict(s, "football", kickoff=kickoff_map.get(s.match_id, ""))
        for s in football
    ] if football else existing.get("football", [])

    if tennis:
        tennis_data = [
            _signal_to_dict(
                s, "tennis",
                tour=tennis_tour_map.get(s.match_id, ""),
                kickoff=kickoff_map.get(s.match_id, ""),
            )
            for s in tennis
        ]
    else:
        tennis_data = existing.get("tennis", [])

    if schedule is not None:
        schedule_data = schedule
    else:
        schedule_data = existing.get("schedule", [])

    if all_odds is not None:
        all_odds_data = all_odds
    else:
        all_odds_data = existing.get("all_odds", {})

    if model_tips is not None:
        model_tips_data = model_tips
    else:
        model_tips_data = existing.get("model_tips", {})

    # Compute bankroll state from ledger
    _resolved_open_bets = open_bets if open_bets is not None else existing.get("open_bets", [])
    _staked = sum(float(b.get("stake", 0)) for b in (_resolved_open_bets or []))
    _max_win = sum(
        float(b.get("stake", 0)) * (float(b.get("current_odds") or b.get("entry_odds", 0)) - 1)
        for b in (_resolved_open_bets or [])
        if b.get("current_odds") or b.get("entry_odds")
    )
    _bankroll_start = 100.0
    _pnl_closed = sum(float(row.get("pnl", 0)) for row in _get_closed_bets())
    _free = round(_bankroll_start + _pnl_closed - _staked, 2)
    _exposure_pct = round(_staked / _bankroll_start * 100, 1)

    payload = {
        "updated":        updated,
        "schedule":       schedule_data,
        "all_odds":       all_odds_data,
        "model_tips":     model_tips_data,
        "football":       football_data,
        "tennis":         tennis_data,
        "portfolio":      portfolio if portfolio else existing.get("portfolio", {}),
        "top_elo":        [{"name": n, "rating": round(r)} for n, r in top_elo] if top_elo else existing.get("top_elo", []),
        "history":        _build_history(),
        "open_bets":      _resolved_open_bets,
        "bankroll_state": {
            "start":        _bankroll_start,
            "free":         round(_free, 2),
            "staked":       round(_staked, 2),
            "exposure_pct": _exposure_pct,
            "max_win":      round(_max_win, 2),
            "pnl_closed":   round(_pnl_closed, 2),
        },
        "wm_stats": _build_wm_stats(),
    }
    payload["odds_history"] = odds_history if odds_history is not None else existing.get("odds_history", {})

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
