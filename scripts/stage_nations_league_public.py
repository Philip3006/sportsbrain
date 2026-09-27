"""Validate a verified runtime shadow artifact and stage its public projection."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.notifications.nations_league_public import (
    NationsLeaguePublicError,
    load_public_nations_league_from_runtime,
    validate_public_nations_league,
)
from src.notifications.public_serializer import assert_no_private_fields


def stage_public_product(path: Path, public_bundle: dict) -> None:
    """Atomically replace only the local static public JSON after validation."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeaguePublicError(
            "static public signals JSON is unavailable"
        ) from exc
    if not isinstance(current, dict):
        raise NationsLeaguePublicError("static public signals JSON is not an object")
    try:
        assert_no_private_fields(current)
    except AssertionError as exc:
        raise NationsLeaguePublicError(
            "existing public signals JSON contains a private field"
        ) from exc
    current["nations_league"] = validate_public_nations_league(public_bundle)
    encoded = (
        json.dumps(current, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".signals-nl-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", required=True, help="Builder 5 immutable Nations League run id"
    )
    parser.add_argument(
        "--expected-source-sha",
        required=True,
        help="independently verified 40-character source SHA from the shadow handoff",
    )
    args = parser.parse_args(argv)
    try:
        bundle = load_public_nations_league_from_runtime(
            args.run_id, expected_source_sha=args.expected_source_sha
        )
        stage_public_product(ROOT / "docs" / "data" / "signals.json", bundle)
    except NationsLeaguePublicError as exc:
        print(f"Nations League public staging blocked: {exc}", file=sys.stderr)
        return 2
    print(
        f"Staged verified Nations League shadow ({bundle['fixture_count']} fixtures, "
        f"artifact {bundle['artifact_digest']}) in docs/data/signals.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
