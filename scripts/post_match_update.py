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

from src.betting.ledger import settle_from_results
from src.notifications.web_dashboard import write_signals_json_all_users, list_known_users


def main() -> None:
    print("Settling open bets against latest results (all users)...")
    for u in list_known_users():
        n = settle_from_results(user=u)
        print(f"  {u}: settled {n} bet(s)")

    print("Refreshing signals.json (portfolio + signal cleanup, all users)...")
    write_signals_json_all_users()  # keeps existing signals, removes expired ones, uploads to cloud
    print("  Done.")


if __name__ == "__main__":
    main()
