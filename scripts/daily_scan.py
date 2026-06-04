"""
CLI entry point for daily WM 2026 value scan.
Usage:
  python scripts/daily_scan.py              # interactive confirmation before logging
  python scripts/daily_scan.py --mock       # dry-run with mock data
  python scripts/daily_scan.py --auto-log   # skip confirmation, log all signals
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scanner.daily_scan import run_daily_scan
from src.betting.ledger import append_bets, LEDGER_PATH


def _confirm_bets(selected_signals: list, bankroll: float) -> list:
    """Shows top signals and asks for confirmation before logging."""
    if not selected_signals:
        return []

    print("\n=== Offene Slots — Bestätigung erforderlich ===")
    confirmed = []
    for s in selected_signals:
        stake = s.stake_pct * bankroll
        print(
            f"\n  {s.home} vs {s.away} | {s.market.upper()} | "
            f"@ {s.decimal_odds:.2f} | EV +{s.ev*100:.1f}% | "
            f"€{stake:.2f} | {s.confidence}"
        )
        ans = input("  Wette eingehen? (j/n): ").strip().lower()
        if ans == "j":
            confirmed.append(s)
            print("  ✓ Eingetragen.")
        else:
            print("  – Übersprungen.")

    return confirmed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SportsBrain daily value scan")
    parser.add_argument("--bankroll", type=float, default=100.0, help="Bankroll in EUR")
    parser.add_argument("--mock", action="store_true", help="Use mock data (no API call)")
    parser.add_argument("--output", type=str, default=None, help="Output path for report")
    parser.add_argument("--auto-log", action="store_true",
                        help="Skip confirmation, log all signals automatically")
    parser.add_argument("--retrain", action="store_true",
                        help="Auto-retrain DC + LightGBM before scanning")
    args = parser.parse_args()

    if args.retrain:
        import subprocess
        print("--- Auto-Retraining ---")
        subprocess.run([sys.executable, "scripts/auto_retrain.py"], check=True)
        print("--- Scan ---")

    signals_df, selected_signals = run_daily_scan(
        bankroll=args.bankroll,
        mock=args.mock,
        output_path=Path(args.output) if args.output else None,
        auto_log=args.auto_log,
    )

    if not signals_df.empty:
        print("\n=== Value Bets ===")
        print(signals_df.to_string(index=False))
    else:
        print("\nNo value bets found today.")

    # Interactive confirmation (skipped with --auto-log)
    if not args.auto_log and selected_signals:
        confirmed = _confirm_bets(selected_signals, args.bankroll)
        if confirmed:
            n = append_bets(confirmed, args.bankroll, LEDGER_PATH)
            print(f"\n{n} Wette(n) ins Ledger eingetragen.")
        else:
            print("\nKeine Wetten eingetragen.")
