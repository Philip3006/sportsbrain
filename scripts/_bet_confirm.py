"""Shared interactive Bet-Confirmation-Gate.

Used by scripts/daily_scan.py and scripts/tennis_scan.py to enforce the
"never auto-log without user confirmation" rule (feedback_no_auto_log).
"""
from __future__ import annotations

import sys


def confirm_bets(selected_signals: list, bankroll: float) -> list:
    """Shows signals and asks j/n per bet. Returns confirmed subset.

    Non-interactive (no TTY): returns [] with an explanatory message.
    Callers that want to bypass the prompt should use an --auto-log flag
    at their own layer, not this function.
    """
    if not selected_signals:
        return []

    if not sys.stdin.isatty():
        print(
            "\n  [Kein interaktives Terminal — Bestätigung übersprungen.]"
            "\n  Nutze '--auto-log' um alle Signals automatisch einzutragen, "
            "oder starte den Scan interaktiv im Terminal."
        )
        return []

    print("\n=== Offene Slots — Bestätigung erforderlich ===")
    confirmed = []
    for s in selected_signals:
        stake = s.stake_eur if getattr(s, "stake_eur", 0) > 0 else s.stake_pct * bankroll
        korr = getattr(s, "stake_reason", "") or ""
        korr_marker = f"  ⚠ KORR-↓ [{korr}]" if korr else ""
        print(
            f"\n  {s.home} vs {s.away} | {s.market.upper()} | "
            f"@ {s.decimal_odds:.2f} | EV +{s.ev*100:.1f}% | "
            f"€{stake:.2f} | {s.confidence}{korr_marker}"
        )
        try:
            ans = input("  Wette eingehen? (j/n): ").strip().lower()
        except EOFError:
            print("\n  [Stdin geschlossen — Bestätigung abgebrochen.]")
            break
        if ans == "j":
            confirmed.append(s)
            print("  ✓ Eingetragen.")
        else:
            print("  – Übersprungen.")

    return confirmed
