#!/usr/bin/env python3
"""Read-only Top-5 public delivery acceptance and publication precheck.

All inputs are captured/generated JSON.  This command performs no network
request and cannot publish, activate, deploy, mutate Cloudflare, consume
provider quota, write a ledger, or place a bet.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.football.top5_public_acceptance import (
    TOP5_PUBLIC_DELIVERY_BLOCKED,
    TOP5_PUBLICATION_PRECHECK_BLOCKED,
    publication_precheck,
    validate_public_bundle,
)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"JSON input must be an object: {path}")
    return value


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include timezone")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    accept = subparsers.add_parser("accept", help="validate a generated public bundle")
    accept.add_argument("--bundle", type=Path, required=True)
    accept.add_argument("--now")
    accept.add_argument("--offline-fixture", action="store_true")

    precheck = subparsers.add_parser(
        "precheck", help="bind public delivery to Builder 1 acceptance"
    )
    precheck.add_argument("--bundle", type=Path, required=True)
    precheck.add_argument("--accepted-evidence", type=Path, required=True)
    precheck.add_argument("--delivery-manifest", type=Path)
    precheck.add_argument("--now")

    args = parser.parse_args(argv)
    try:
        now = _now(args.now)
        if args.command == "accept":
            result = validate_public_bundle(
                _read(args.bundle), now=now, offline_fixture=args.offline_fixture
            )
        else:
            result = publication_precheck(
                _read(args.bundle),
                _read(args.accepted_evidence),
                now=now,
                delivery_manifest=(
                    _read(args.delivery_manifest) if args.delivery_manifest else None
                ),
            )
    except (OSError, TypeError, ValueError) as exc:
        result = {
            "status": (
                TOP5_PUBLICATION_PRECHECK_BLOCKED
                if args.command == "precheck"
                else TOP5_PUBLIC_DELIVERY_BLOCKED
            ),
            "reasons": [{"code": "PUBLIC_SCHEMA_INVALID", "message": str(exc)}],
        }
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["status"]
        in {
            "TOP5_PUBLIC_DELIVERY_READY",
            "TOP5_PUBLICATION_PRECHECK_READY",
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
