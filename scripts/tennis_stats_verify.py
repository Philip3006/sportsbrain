"""Verifiziert das Tennis-Abstract matchmx-Column-Mapping gegen bekannte Spieler.

Prüfung: Sanity-Checks auf letzte 20 Matches je Spieler (last_n=20).
Rot wenn:
  - Kein Match gefunden (Slug falsch, TA down, Layout geändert)
  - dominance_rate außerhalb [0.35, 0.75] (Column-Shift wahrscheinlich)
  - ace_rate außerhalb [0.00, 0.20] (Column-Shift wahrscheinlich)

Nutzung:
    python3 scripts/tennis_stats_verify.py
    python3 scripts/tennis_stats_verify.py --player "Novak Djokovic"

Exit-Code 0 wenn alles grün, 1 bei mind. einem Fehler.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.tennis_stats import fetch_aggregate, fetch_match_stats

# Bekannte Top-Spieler mit erwartetem Verhalten
DEFAULT_PLAYERS: list[tuple[str, str]] = [
    ("Carlos Alcaraz", "atp"),  # Top-3, hohe dom
    ("Jack Draper", "atp"),     # Rising, Big Serve → ace_rate hoch
    ("Jannik Sinner", "atp"),   # Top-1
    ("Naomi Osaka", "wta"),     # zurückgekehrt, df_rate typisch hoch
    ("Iga Swiatek", "wta"),     # Top-WTA
]


def _check(player: str, tour: str = "atp") -> tuple[bool, str]:
    stats = fetch_match_stats(player, tour=tour)
    if not stats:
        return False, f"KEINE Matches für {player} ({tour}) — TA down/rate-limited/slug-Fehler"
    agg = fetch_aggregate(player, last_n=20, tour=tour)
    if agg.n_matches == 0:
        return False, f"aggregate n=0 für {player} (roh {len(stats)} matches)"
    if not (0.35 <= agg.dominance_rate <= 0.75):
        return False, (
            f"{player}: dominance_rate={agg.dominance_rate:.3f} außerhalb [0.35,0.75] — "
            "Column-Shift verdächtig"
        )
    if not (0.0 <= agg.ace_rate <= 0.20):
        return False, (
            f"{player}: ace_rate={agg.ace_rate:.3f} außerhalb [0.0,0.20] — Column-Shift verdächtig"
        )
    return True, (
        f"OK {player:20s} n={agg.n_matches:3d} "
        f"dom={agg.dominance_rate:.3f} ace={agg.ace_rate:.3f} "
        f"df={agg.df_rate:.3f} wr={agg.win_rate:.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", action="append", default=None,
                    help="Repeat for multiple; default: builtin top-5")
    args = ap.parse_args()

    if args.player:
        # CLI-Overrides sind ATP by default
        players_typed: list[tuple[str, str]] = [(p, "atp") for p in args.player]
    else:
        players_typed = DEFAULT_PLAYERS
    failures = 0
    for p, tour in players_typed:
        ok, msg = _check(p, tour)
        print(msg)
        if not ok:
            failures += 1
    if failures:
        print(f"\n[FAIL] {failures}/{len(players)} Spieler mit Anomalie — TA-Layout prüfen!")
        return 1
    print(f"\n[OK] {len(players)} Spieler verifiziert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
