"""Validate an offline Champions League public snapshot.

This command is intentionally read-only.  It accepts a locally supplied
snapshot, applies the same public serializer used by the publication path, and
then evaluates the CL release, provenance, and freshness gates.  It never
contacts a provider, reads credentials, writes runtime state, or publishes.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.champions_league_publication import (
    ChampionsLeaguePublicationError,
    validate_champions_league_publication,
)
from src.notifications.public_serializer import serialize_public_product


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(timezone.utc)


def evaluate_snapshot(
    snapshot: dict[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
    require_published: bool = False,
) -> dict[str, Any]:
    """Return acceptance facts or raise on any incomplete/stale CL evidence."""
    public = serialize_public_product(snapshot)
    facts = validate_champions_league_publication(
        public,
        now=now,
        max_age_seconds=max_age_seconds,
        require_release=True,
        require_health=True,
        require_published=require_published,
    )
    return {
        "status": "CL_PUBLICATION_ACCEPTANCE_READY",
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "max_age_seconds": max_age_seconds,
        **facts,
    }


def _load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot must be a JSON object")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="offline JSON public snapshot")
    parser.add_argument(
        "--now",
        required=True,
        help="observation time used for deterministic freshness validation",
    )
    parser.add_argument("--max-age-seconds", type=float, default=900.0)
    parser.add_argument("--require-published", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = evaluate_snapshot(
            _load_snapshot(args.snapshot),
            now=_parse_timestamp(args.now),
            max_age_seconds=args.max_age_seconds,
            require_published=args.require_published,
        )
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        ChampionsLeaguePublicationError,
    ) as exc:
        print(
            json.dumps(
                {"status": "CL_PUBLICATION_ACCEPTANCE_BLOCKED", "reason": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
