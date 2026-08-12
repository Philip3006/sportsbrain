"""
O1-4 Tennis Ledger Integrity Backfill.

Corrects the 8 tennis bets written with league='wm2026' due to the missing
BetSignal.league propagation in tennis_detector.py (root cause: line 314
of ledger.py used 'wm2026' as empty-string fallback).

Usage:
    python3 scripts/backfill_tennis_league.py          # dry-run (default, safe)
    python3 scripts/backfill_tennis_league.py --commit  # write corrections

Audit trail written to:
    results/ledger_corrections/2026-08-12_tennis_league_backfill.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
LEDGER = ROOT / "results" / "ledger_philip.csv"
AUDIT_DIR = ROOT / "results" / "ledger_corrections"
SIGNAL_HISTORY = ROOT / "data" / "cache" / "signal_history.jsonl"

# Manual ground-truth: ATP/WTA classification from player names.
# Verified 2026-08-12 via signal_history.jsonl + public tennis records.
_TOUR_BY_PLAYER: dict[str, str] = {
    # ATP players
    "Aleksandar Vukic": "atp", "Daniel Altmaier": "atp",
    "Adam Walton": "atp", "Jenson Brooksby": "atp",
    "James Duckworth": "atp", "Alex de Minaur": "atp",
    "Frances Tiafoe": "atp", "Marin Cilic": "atp",
    "Valentin Royer": "atp", "Tommy Paul": "atp",
    # WTA players
    "Nikola Bartunkova": "wta", "Bianca Andreescu": "wta",
    "Panna Udvardy": "wta", "Naomi Osaka": "wta",
    "Alycia Parks": "wta", "Alexandra Eala": "wta",
}

_TENNIS_MARKETS = ("o/u_games_", "o/u_sets_", "score_", "first_set_")


def _classify_tour(home: str, away: str, signal_history: list[dict]) -> str:
    """Determine tour from player lookup, then signal_history cross-check."""
    # Direct lookup
    for player in (home, away):
        if player in _TOUR_BY_PLAYER:
            return _TOUR_BY_PLAYER[player]
    # Cross-reference signal_history
    for entry in signal_history:
        if entry.get("home") == home and entry.get("away") == away:
            tour = entry.get("tour", "")
            if tour in ("atp", "wta"):
                return tour
    raise ValueError(f"Cannot determine tour for {home} vs {away}")


def _load_signal_history() -> list[dict]:
    if not SIGNAL_HISTORY.exists():
        return []
    rows = []
    with open(SIGNAL_HISTORY) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def run(commit: bool) -> int:
    signal_history = _load_signal_history()
    rows = []
    with open(LEDGER) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for row in reader:
            rows.append(dict(row))

    # Find affected rows
    affected: list[tuple[int, dict, str]] = []  # (index, row, corrected_league)
    for i, row in enumerate(rows):
        if row.get("league") != "wm2026":
            continue
        market = row.get("market", "")
        if not any(market.startswith(p) for p in _TENNIS_MARKETS):
            continue
        home = row.get("home", "")
        away = row.get("away", "")
        try:
            corrected = _classify_tour(home, away, signal_history)
        except ValueError as exc:
            print(f"[WARN] Cannot classify {home} vs {away}: {exc}", file=sys.stderr)
            continue
        affected.append((i, row, corrected))

    if not affected:
        print("No rows to correct. All tennis bets already have correct league.")
        return 0

    print(f"Found {len(affected)} rows to correct:")
    audit_records = []
    ts = datetime.now(timezone.utc).isoformat()
    for i, row, corrected in affected:
        print(
            f"  [{i}] {row['home']} vs {row['away']} | {row['market']} | "
            f"wm2026 → {corrected} | status={row['status']} | date={row['placed_date']}"
        )
        audit_records.append({
            "correction_ts": ts,
            "bet_id": row.get("match_id", ""),
            "row_index": i,
            "home": row["home"],
            "away": row["away"],
            "market": row["market"],
            "status": row["status"],
            "placed_date": row["placed_date"],
            "previous_value": "wm2026",
            "corrected_value": corrected,
            "reason": "league-default-bug ledger.py:314 — tennis_detector._signal() did not set BetSignal.league",
            "evidence": "player-name lookup + signal_history.jsonl cross-check",
        })

    if not commit:
        print("\nDRY-RUN: no changes written. Re-run with --commit to apply.")
        return len(affected)

    # Apply corrections
    for i, row, corrected in affected:
        rows[i]["league"] = corrected

    # Write corrected ledger
    with open(LEDGER, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nLedger updated: {len(affected)} rows corrected.")

    # Write audit trail
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit_file = AUDIT_DIR / "2026-08-12_tennis_league_backfill.jsonl"
    with open(audit_file, "a") as f:
        for record in audit_records:
            f.write(json.dumps(record) + "\n")
    print(f"Audit trail written to {audit_file}")

    return len(affected)


def main() -> None:
    parser = argparse.ArgumentParser(description="O1-4 Tennis Ledger League Backfill")
    parser.add_argument("--commit", action="store_true", help="Apply corrections (default: dry-run)")
    args = parser.parse_args()
    n = run(commit=args.commit)
    print(f"\n{'Corrected' if args.commit else 'Would correct'}: {n} rows")


if __name__ == "__main__":
    main()
