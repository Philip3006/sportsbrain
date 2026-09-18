"""One-shot, explicitly authorized candidate-only LL/ESP1 capture entrypoint.

Default mode is offline-disabled.  A real request requires both an external
authorization JSON file and ``--execute-network``.  The script never prints
the provider key or raw response headers.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.football.odds.therundown_la_liga_capture import (
    LaLigaCaptureAuthorization,
    capture_la_liga,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authorization-file",
        type=Path,
        help="external operator-supplied authorization JSON; never commit this file",
    )
    parser.add_argument(
        "--execute-network",
        action="store_true",
        help="required in addition to authorization-file for the one-shot real path",
    )
    parser.add_argument(
        "--now",
        help="offline test clock in ISO-8601 form; defaults to current UTC",
    )
    args = parser.parse_args()

    if not args.execute_network or args.authorization_file is None:
        print(
            json.dumps(
                {
                    "status": "DISABLED",
                    "reason": "authorization_file_and_execute_network_required",
                    "provider": "therundown_experimental",
                    "league": "LL",
                    "real_requests": 0,
                },
                sort_keys=True,
            )
        )
        return 0
    try:
        raw = json.loads(args.authorization_file.read_text())
        authorization = LaLigaCaptureAuthorization.from_payload(raw)
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(timezone.utc)
        )
        result = capture_la_liga(authorization, now=now)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": type(exc).__name__}))
        return 1
    print(json.dumps(result.as_payload(), sort_keys=True))
    return 0 if result.status.value == "CAPTURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
