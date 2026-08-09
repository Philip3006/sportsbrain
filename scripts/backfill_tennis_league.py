"""
O1-4 Backfill: Correct league="wm2026" → "atp"/"wta" for tennis bets in ledger.

Root cause: ledger.py:314 used `or "wm2026"` as default when BetSignal.league was
unset. Tennis signals never set league (fixed in O1-4 via tennis_detector.py).
Result: 11 tennis bets from 2026-08-03 to 2026-08-06 have league="wm2026".

Evidence source: signal_history.jsonl player-name lookup (tour field confirmed).
Identification method: league="wm2026" + player names matching tennis player list
from signal_history. WM football bets with AH markets are excluded (they have
national team names as home/away, not individual players).

Usage:
    python3 scripts/backfill_tennis_league.py            # dry-run (safe)
    python3 scripts/backfill_tennis_league.py --apply    # write to ledger copy
    python3 scripts/backfill_tennis_league.py --apply --ledger path/to/ledger.csv

IMPORTANT: Do NOT run with --apply on production ledger during Phase-0 window.
Production backfill requires explicit CEO approval post-Phase-0 exit (ROADMAP O.11).
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import pandas as pd

_log = logging.getLogger("sportsbrain.backfill_tennis_league")

# Repo root (parent of scripts/)
_REPO_ROOT = Path(__file__).resolve().parent.parent

_SIGNAL_HISTORY = _REPO_ROOT / "data" / "cache" / "signal_history.jsonl"


def _load_tour_lookup(signal_history_path: Path) -> dict[tuple[str, str], str]:
    """Build (home, away) → tour lookup from signal_history.jsonl."""
    lookup: dict[tuple[str, str], str] = {}
    if not signal_history_path.exists():
        return lookup
    with signal_history_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("sport") != "tennis":
                continue
            tour = row.get("tour", "")
            home = row.get("home", "")
            away = row.get("away", "")
            if tour and home and away:
                lookup[(home, away)] = tour
    return lookup


def _is_tennis_player_pair(home: str, away: str, tour_lookup: dict) -> str | None:
    """Return tour if (home, away) is a known tennis player pair, else None."""
    return tour_lookup.get((home, away))


def find_affected_rows(
    df: pd.DataFrame,
    tour_lookup: dict[tuple[str, str], str],
) -> list[dict]:
    """
    Returns list of correction records for rows where:
      - league == "wm2026"
      - (home, away) appears in tennis signal_history with a tour value

    Each record contains: row_index, match_id, home, away, market, placed_date,
    old_league, new_league, evidence.
    """
    corrections = []
    for idx, row in df.iterrows():
        if row.get("league") != "wm2026":
            continue
        home = str(row.get("home", ""))
        away = str(row.get("away", ""))
        tour = _is_tennis_player_pair(home, away, tour_lookup)
        if tour is None:
            continue
        corrections.append({
            "row_index": int(idx),
            "match_id": str(row.get("match_id", "")),
            "home": home,
            "away": away,
            "market": str(row.get("market", "")),
            "placed_date": str(row.get("placed_date", "")),
            "old_league": "wm2026",
            "new_league": tour,
            "reason": (
                f"BetSignal.league was empty at append time (ledger.py:314 used "
                f"'or wm2026' default). Signal for {home} vs {away} is a tennis "
                f"match (tour={tour}) — confirmed via signal_history.jsonl player-name lookup."
            ),
            "evidence": f"signal_history.jsonl ({home}, {away}) → tour={tour}",
        })
    return corrections


def apply_corrections(
    df: pd.DataFrame,
    corrections: list[dict],
    output_path: Path,
) -> None:
    """Write corrected DataFrame to output_path. Fails if any expected row is gone."""
    for c in corrections:
        idx = c["row_index"]
        if idx not in df.index:
            raise RuntimeError(
                f"Backfill safety check failed: row_index {idx} not in ledger. "
                "Source ledger may have changed since dry-run. Re-run dry-run first."
            )
        actual_old = df.at[idx, "league"]
        if actual_old != c["old_league"]:
            raise RuntimeError(
                f"Backfill safety check failed at row {idx}: "
                f"expected old_league={c['old_league']!r} but found {actual_old!r}. "
                "Ledger state changed. Re-run dry-run."
            )
        df.at[idx, "league"] = c["new_league"]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"  Written: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply corrections to a COPY of the ledger (not the production file).",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Path to ledger CSV. Default: results/ledger_philip.csv",
    )
    parser.add_argument(
        "--signal-history",
        type=Path,
        default=_SIGNAL_HISTORY,
        help="Path to signal_history.jsonl for evidence lookup.",
    )
    args = parser.parse_args()

    ledger_path = args.ledger or (_REPO_ROOT / "results" / "ledger_philip.csv")
    if not ledger_path.exists():
        print(f"ERROR: Ledger not found: {ledger_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(ledger_path, dtype=str)
    tour_lookup = _load_tour_lookup(args.signal_history)

    print(f"Ledger: {ledger_path} ({len(df)} rows)")
    print(f"Signal history: {args.signal_history} ({len(tour_lookup)} tennis player-pair entries)")
    print()

    corrections = find_affected_rows(df, tour_lookup)

    if not corrections:
        print("No affected rows found. Nothing to correct.")
        return 0

    print(f"=== {len(corrections)} row(s) require correction ===")
    print()
    for c in corrections:
        print(f"  row {c['row_index']:3d} | {c['placed_date']} | {c['home']} vs {c['away']}")
        print(f"         market={c['market']}")
        print(f"         old_league={c['old_league']!r}  →  new_league={c['new_league']!r}")
        print(f"         evidence: {c['evidence']}")
        print()

    if not args.apply:
        print("=== DRY RUN — no files modified ===")
        print("Re-run with --apply to write corrections to a ledger copy.")
        print()
        print("IMPORTANT: Production backfill requires CEO approval post-Phase-0 exit (ROADMAP O.11).")
        return 0

    # Apply to a copy, never to the original
    copy_path = ledger_path.with_suffix(".backfill_tennis_league.csv")
    shutil.copy2(ledger_path, copy_path)
    print(f"=== APPLY MODE — writing to COPY: {copy_path} ===")
    print("  (Production file unchanged)")
    print()

    apply_corrections(df, corrections, copy_path)

    print(f"=== Done: {len(corrections)} row(s) corrected ===")
    print()
    print("Review the copy before replacing production:")
    print(f"  diff <(python3 -c \"import pandas as pd; df=pd.read_csv('{ledger_path}'); print(df[['match_id','league']].to_string())\") \\")
    print(f"       <(python3 -c \"import pandas as pd; df=pd.read_csv('{copy_path}'); print(df[['match_id','league']].to_string())\")")
    print()
    print("To promote the copy to production (CEO approval required post-Phase-0):")
    print(f"  cp '{copy_path}' '{ledger_path}'")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
