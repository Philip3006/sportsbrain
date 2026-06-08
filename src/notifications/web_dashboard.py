"""
Writes docs/data/signals.json for the GitHub Pages web dashboard.
Called at the end of daily_scan.py and tennis_scan.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.betting.value_detector import BetSignal

ROOT = Path(__file__).parent.parent.parent
_JSON_PATH = ROOT / "docs" / "data" / "signals.json"


def _signal_to_dict(s: BetSignal, sport: str = "football") -> dict:
    return {
        "sport":      sport,
        "match":      f"{s.home} vs {s.away}",
        "market":     s.market,
        "odds":       round(s.decimal_odds, 2),
        "model_prob": round(s.model_prob * 100, 1),
        "ev_pct":     round(s.ev * 100, 1),
        "stake_eur":  round(s.stake_eur, 2),
        "confidence": s.confidence,
    }


def write_signals_json(
    football: list[BetSignal] | None = None,
    tennis: list[BetSignal] | None = None,
    portfolio: dict | None = None,
    top_elo: list[tuple[str, float]] | None = None,
) -> None:
    """
    Writes (or merges into) docs/data/signals.json.
    Merges football and tennis so each scanner can call independently.
    """
    football = football or []
    tennis = tennis or []
    portfolio = portfolio or {}
    top_elo = top_elo or []

    # Load existing JSON to merge sport sections
    existing: dict = {}
    if _JSON_PATH.exists():
        try:
            existing = json.loads(_JSON_PATH.read_text())
        except Exception:
            existing = {}

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    football_data = [_signal_to_dict(s, "football") for s in football] if football else existing.get("football", [])
    tennis_data = [_signal_to_dict(s, "tennis") for s in tennis] if tennis else existing.get("tennis", [])

    payload = {
        "updated": updated,
        "football": football_data,
        "tennis":   tennis_data,
        "portfolio": portfolio if portfolio else existing.get("portfolio", {}),
        "top_elo": [{"name": n, "rating": round(r)} for n, r in top_elo] if top_elo else existing.get("top_elo", []),
    }

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
