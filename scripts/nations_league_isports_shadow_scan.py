"""Run one immutable, non-betting iSports Nations League shadow capture."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scanner.nations_league_isports_shadow import (
    NationsLeagueIsportsError,
    run_isports_shadow_scan,
)


def main() -> int:
    try:
        artifact, path = run_isports_shadow_scan()
    except NationsLeagueIsportsError as exc:
        statuses = ",".join(str(status) for status in exc.http_statuses) or "none"
        print(
            f"iSports Nations League shadow blocked: {exc} "
            f"(requests={exc.request_count}, HTTP statuses={statuses})",
            file=sys.stderr,
        )
        return 2
    print(
        f"iSports shadow artifact: {path} "
        f"(requests={artifact['request_count']}, retries={artifact['retry_count']}, "
        f"fixtures={artifact['fixture_count']}, covered={artifact['covered_fixture_count']}, "
        f"digest={artifact['artifact_digest']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
