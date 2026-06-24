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
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.settle_bets import fetch_scores, settle_market
from src.scanner.output import SIGNAL_HISTORY

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


def _resolve_outcome(row: dict, scores: dict) -> str | None:
    """Look up score for this signal's match and call settle_market()."""
    home, away = row.get("home", ""), row.get("away", "")
    mid = row.get("match_id", "")
    sc = scores.get(mid) or scores.get(f"{home} vs {away}")
    if not sc:
        return None
    market = row.get("market", "")
    if market.startswith("scorer_"):
        return None  # scorer markets need ESPN goal data — skip for now
    return settle_market(market, sc["home_score"], sc["away_score"])


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
    scores = fetch_scores()
    print(f"[backfill] {len(scores) // 2} abgeschlossene Matches via API")

    resolved = 0
    now_ts = datetime.now(timezone.utc).isoformat()
    for r in rows:
        if r.get("outcome") is not None:
            continue
        if r.get("scan_date", "") >= today:
            continue
        outcome = _resolve_outcome(r, scores)
        if outcome is None:
            continue
        if not dry_run:
            r["outcome"] = outcome
            r["outcome_ts"] = now_ts
        else:
            print(f"  [dry] {r.get('home')} vs {r.get('away')} [{r.get('market')}] → {outcome}")
        resolved += 1

    print(f"[backfill] {resolved} Outcomes aufgelöst" + (" (dry-run)" if dry_run else ""))

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
