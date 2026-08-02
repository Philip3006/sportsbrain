"""Historic Odds Snapshot Pipeline (Hebel 4 Fundament).

Persistiert Odds pro Match zu 3 Timestamps: T-24h, T-1h, T-0 (Kickoff).
Speicherformat: `data/odds_history/tennis/{match_id}.json`

Nach Match-Ende kann `compute_clv_from_snapshots()` die Line-Movement
berechnen und ins Ledger als CLV-Metrik zurückführen.

Verwendung via Cron (nicht implementiert hier — externer Aufruf):
  # T-24h Snapshot
  python3 scripts/tennis_odds_snapshot.py --hours-before 24
  # T-1h Snapshot
  python3 scripts/tennis_odds_snapshot.py --hours-before 1
  # T-0 (kurz vor Match)
  python3 scripts/tennis_odds_snapshot.py --hours-before 0
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent.parent
SNAPSHOT_DIR = _ROOT / "data" / "odds_history" / "tennis"


def _snapshot_path(match_id: str) -> Path:
    return SNAPSHOT_DIR / f"{match_id}.json"


def save_snapshot(
    match_id: str,
    *,
    odds_a: float,
    odds_b: float,
    source: str,
    label: str,
    tournament_slug: str | None = None,
    player_a: str | None = None,
    player_b: str | None = None,
) -> None:
    """Fügt einen Snapshot in die pro-Match Odds-Historie.

    label: 'T-24h' | 'T-1h' | 'T-0' | 'closing' — freier String.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = _snapshot_path(match_id)
    if p.exists():
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("match_id", match_id)
    if player_a: data["player_a"] = player_a
    if player_b: data["player_b"] = player_b
    if tournament_slug: data["tournament_slug"] = tournament_slug
    snapshots = data.setdefault("snapshots", [])
    snapshots.append({
        "label": label,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "odds_a": float(odds_a),
        "odds_b": float(odds_b),
        "source": source,
    })
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def load_snapshots(match_id: str) -> list[dict]:
    p = _snapshot_path(match_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("snapshots", [])
    except Exception:
        return []


def compute_clv_from_snapshots(match_id: str) -> Optional[dict]:
    """Berechnet CLV aus Opening-Snapshot vs Closing-Snapshot.

    Returns dict mit line_move_pct_a/b und clv_available oder None.
    """
    from src.tennis.line_movement import compute_line_move
    snaps = load_snapshots(match_id)
    if len(snaps) < 2:
        return None
    # Chronologisch sortieren
    snaps = sorted(snaps, key=lambda s: s.get("ts_utc", ""))
    opening = snaps[0]
    closing = snaps[-1]
    lm_a = compute_line_move(opening["odds_a"], closing["odds_a"])
    lm_b = compute_line_move(opening["odds_b"], closing["odds_b"])
    return {
        "opening_ts": opening.get("ts_utc"),
        "closing_ts": closing.get("ts_utc"),
        "opening_odds_a": opening["odds_a"],
        "closing_odds_a": closing["odds_a"],
        "line_move_pct_a": lm_a.move_pct if lm_a else None,
        "line_move_pct_b": lm_b.move_pct if lm_b else None,
        "sharp_dir_a": lm_a.sharp_direction if lm_a else 0,
        "sharp_dir_b": lm_b.sharp_direction if lm_b else 0,
        "n_snapshots": len(snaps),
    }
