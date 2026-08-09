"""
Backfill outcomes for archived signals in data/cache/signal_history.jsonl.

Reads entries without outcome where scan_date < today, fetches scores via
fetch_scores() from settle_bets, determines outcome via settle_market(), and
rewrites signal_history.jsonl. Also aggregates signal_performance.json.

CLI: python3 scripts/backfill_signal_outcomes.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.settle_bets import fetch_scores, settle_market
from src.scanner.output import SIGNAL_HISTORY
from src.betting.tennis_settlement import settle_tennis_market
from src.data.tennis_scores import fetch_tennis_scores
from src.tennis.backfill_helpers import GHOST_AGE_DAYS as TENNIS_GHOST_AGE_DAYS, fetch_espn_window, lookup_tennis_score
from src.tennis.discovery import discover_active_tournaments
from src.football.backfill_helpers import (
    GHOST_AGE_DAYS as FOOTBALL_GHOST_AGE_DAYS,
    fetch_bl2_window,
    is_ghost_bl2,
)

SIGNAL_PERF = ROOT / "data" / "cache" / "signal_performance.json"


def _load_signals() -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    rows = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _save_signals(rows: list[dict]) -> None:
    SIGNAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_HISTORY.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _resolve_outcome(row: dict, scores: dict, tennis_scores: dict) -> str | None:
    """Look up score for this signal's match and call the sport-appropriate settler."""
    home, away = row.get("home", ""), row.get("away", "")
    mid = row.get("match_id", "")
    market = row.get("market", "")
    sport = row.get("sport", "football")

    if sport == "tennis":
        sc = lookup_tennis_score(home, away, mid, tennis_scores)
        if not sc:
            return None
        result = settle_tennis_market(market, sc)
        return None if result == "pending" else result

    sc = scores.get(mid) or scores.get(f"{home} vs {away}")
    if not sc:
        return None
    if market.startswith("scorer_"):
        return None  # scorer markets need ESPN goal data — skip for now
    return settle_market(market, sc["home_score"], sc["away_score"])


def _is_ghost_football(row: dict) -> bool:
    """True if a football signal involves a team not in the current league."""
    league = row.get("league", "")
    home, away = row.get("home", ""), row.get("away", "")
    if league == "bl2":
        return is_ghost_bl2(home, away)
    return False


def backfill(dry_run: bool = False) -> dict:
    rows = _load_signals()
    if not rows:
        print("[backfill] Keine Signale in signal_history.jsonl — abbruch.")
        return {}

    today = date.today().isoformat()
    pending = [r for r in rows if r.get("outcome") is None and r.get("scan_date", "") < today]
    if not pending:
        print(f"[backfill] {len(rows)} Signale, keine offenen Outcomes — nichts zu tun.")
        _aggregate_and_save(rows, dry_run)
        return {}

    print(f"[backfill] {len(pending)}/{len(rows)} Signale ohne Outcome — hole Scores...")
    has_football = any(r.get("sport", "football") == "football" for r in pending)
    has_bl2 = any(r.get("league") == "bl2" for r in pending)
    has_tennis = any(r.get("sport") == "tennis" for r in pending)
    scores: dict = {}
    tennis_scores: dict = {}
    if has_football:
        scores = fetch_scores()
        print(f"[backfill] Football: {len(scores) // 2} abgeschlossene Matches (TheOddsAPI/ESPN-WC)")
    if has_bl2:
        pending_bl2 = [r for r in pending if r.get("league") == "bl2"]
        scan_dates_bl2 = sorted(set(r.get("scan_date", "")[:10] for r in pending_bl2 if r.get("scan_date")))
        scores, n_bl2_dates = fetch_bl2_window(scan_dates_bl2, scores)
        bl2_espn_count = sum(1 for v in scores.values() if isinstance(v, dict) and v.get("source") == "espn_bl2")
        print(f"[backfill] BL2 ESPN: {bl2_espn_count // 2} Matches geladen ({n_bl2_dates} Tage)")
    if has_tennis:
        try:
            tourneys = discover_active_tournaments()
            sport_keys = [k for t in tourneys for k in t.sport_keys]
        except Exception:
            sport_keys = None
        tennis_scores = fetch_tennis_scores(sport_keys)
        # Scan-Dates + 7-Tage-Window: Matches werden oft erst nach dem Scan gespielt
        pending_tennis = [r for r in pending if r.get("sport") == "tennis"]
        scan_dates_raw = sorted(set(r.get("scan_date", "")[:10] for r in pending_tennis if r.get("scan_date")))
        tennis_scores, n_dates = fetch_espn_window(scan_dates_raw, tennis_scores)
        print(f"[backfill] Tennis: {sum(1 for v in tennis_scores.values() if v.get('sets'))} Matches mit Set-Daten ({n_dates} ESPN-Tage)")

    resolved = 0
    ghosted = 0
    now_ts = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if r.get("outcome") is not None:
            continue
        scan_date_str = r.get("scan_date", "")
        if scan_date_str >= today:
            continue
        outcome = _resolve_outcome(r, scores, tennis_scores)
        if outcome is None:
            try:
                age = (date.today() - date.fromisoformat(scan_date_str)).days
            except ValueError:
                age = 0
            sport = r.get("sport", "football")
            is_ghost = False
            if sport == "tennis" and age >= TENNIS_GHOST_AGE_DAYS:
                is_ghost = True
            elif sport == "football" and _is_ghost_football(r):
                # Team not in current league → ghost immediately
                is_ghost = True
            elif sport == "football" and age >= FOOTBALL_GHOST_AGE_DAYS:
                is_ghost = True
            if is_ghost:
                if not dry_run:
                    r["outcome"] = "ghost"
                    r["outcome_ts"] = now_ts
                else:
                    print(f"  [ghost] {r.get('home')} vs {r.get('away')} [{r.get('market')}]")
                ghosted += 1
            continue
        if not dry_run:
            r["outcome"] = outcome
            r["outcome_ts"] = now_ts
        else:
            print(f"  [dry] {r.get('home')} vs {r.get('away')} [{r.get('market')}] → {outcome}")
        resolved += 1
    msg = f"[backfill] {resolved} Outcomes aufgelöst"
    if ghosted:
        msg += f", {ghosted} Ghost-Signale markiert"
    print(msg + (" (dry-run)" if dry_run else ""))

    if not dry_run:
        _save_signals(rows)

    perf = _aggregate_and_save(rows, dry_run)
    return perf


def _aggregate_and_save(rows: list[dict], dry_run: bool = False) -> dict:
    by_market: dict[str, dict] = defaultdict(lambda: {
        "n": 0, "n_placed": 0, "n_outcome": 0,
        "n_won": 0, "ev_sum": 0.0,
    })
    by_conf: dict[str, dict] = defaultdict(lambda: {"n": 0, "n_won": 0})

    for r in rows:
        mkt = r.get("market", "unknown")
        conf = r.get("confidence", "UNKNOWN")
        outcome = r.get("outcome")
        placed = r.get("placed", False)
        ev_pct = r.get("ev_pct", 0.0)

        by_market[mkt]["n"] += 1
        by_market[mkt]["ev_sum"] += ev_pct
        if placed:
            by_market[mkt]["n_placed"] += 1
        if outcome is not None:
            by_market[mkt]["n_outcome"] += 1
            if outcome == "won":
                by_market[mkt]["n_won"] += 1
        by_conf[conf]["n"] += 1
        if outcome == "won":
            by_conf[conf]["n_won"] += 1

    def _safe_div(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    perf = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_signals_total": len(rows),
        "n_with_outcome": sum(1 for r in rows if r.get("outcome") is not None),
        "by_market": {
            mkt: {
                "n": d["n"],
                "n_placed": d["n_placed"],
                "n_outcome": d["n_outcome"],
                "accuracy": _safe_div(d["n_won"], d["n_outcome"]),
                "ev_mean": round(d["ev_sum"] / d["n"], 2) if d["n"] else None,
            }
            for mkt, d in sorted(by_market.items())
        },
        "by_confidence": {
            conf: {
                "n": d["n"],
                "accuracy": _safe_div(d["n_won"], d["n"]),
            }
            for conf, d in sorted(by_conf.items())
        },
    }

    if not dry_run:
        SIGNAL_PERF.parent.mkdir(parents=True, exist_ok=True)
        SIGNAL_PERF.write_text(json.dumps(perf, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[backfill] signal_performance.json geschrieben ({perf['n_signals_total']} Signale, "
              f"{perf['n_with_outcome']} mit Outcome)")
    else:
        print(f"[backfill dry] Performance: {perf['n_signals_total']} Signale")
        for mkt, d in perf["by_market"].items():
            if d["n_outcome"]:
                print(f"  {mkt}: n={d['n']} acc={d['accuracy']} ev_mean={d['ev_mean']}%")

    return perf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill outcomes for archived signals")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
