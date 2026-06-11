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

    payload = {
        "updated":     updated,
        "schedule":    schedule_data,
        "all_odds":    all_odds_data,
        "model_tips":  model_tips_data,
        "football":    football_data,
        "tennis":      tennis_data,
        "portfolio":   portfolio if portfolio else existing.get("portfolio", {}),
        "top_elo":     [{"name": n, "rating": round(r)} for n, r in top_elo] if top_elo else existing.get("top_elo", []),
        "history":     _build_history(),
        "open_bets":   open_bets if open_bets is not None else existing.get("open_bets", []),
    }

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
