"""
Post-match update: settle open bets + refresh signals.json + upload to Cloudflare.
Run after match results are available (e.g. hourly via closing_odds CI workflow).
Does NOT re-run the full scan — only updates what changed in the ledger.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.betting.ledger import settle_from_results, LEDGER_PATH
from src.notifications.web_dashboard import write_signals_json


def main() -> None:
    print("Settling open bets against latest results...")
    n = settle_from_results(LEDGER_PATH)
    print(f"  Settled: {n} bet(s)")

    print("Refreshing signals.json (portfolio + signal cleanup)...")
    write_signals_json()  # keeps existing signals, removes expired ones, uploads to cloud
    print("  Done.")


if __name__ == "__main__":
    main()
