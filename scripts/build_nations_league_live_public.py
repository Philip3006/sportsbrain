"""Materialize the canonical Nations League LIVE public projection.

This is an offline serializer-boundary command.  It consumes only committed
local release/evidence artifacts and writes atomically; it never contacts a
provider, reads credentials, or performs publication.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_live_runtime import load_live_store
from src.analysis.nations_league_model_lifecycle import active_release_from_registry
from src.notifications.nations_league_live_public import (
    build_live_public_nations_league,
)
from src.notifications.public_serializer import (
    assert_no_private_fields,
    serialize_public_product,
)
from src.utils.atomic_io import atomic_write_json


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _campaign_records(path: Path) -> list[dict]:
    value = _json(path).get("records")
    if not isinstance(value, list) or not value:
        raise TypeError("campaign contains no immutable prediction records")
    return value


def materialize(
    *,
    campaign: Path,
    registry: Path,
    binding: Path,
    inputs: list[Path],
    store: Path | None,
    output: Path,
    as_of: str,
) -> dict:
    release = active_release_from_registry(_json(registry))
    records = _campaign_records(campaign)
    if store is not None:
        records.extend(load_live_store(store))
    bundle = build_live_public_nations_league(
        records,
        active_release=release,
        evidence_binding=_json(binding),
        as_of=as_of,
    )
    snapshots = [_json(path) for path in inputs]
    if not snapshots:
        snapshots = [_json(output)]
    candidate = dict(snapshots[0])
    candidate["nations_league"] = bundle
    public = serialize_public_product(candidate)
    assert_no_private_fields(public)
    atomic_write_json(output, public, indent=2, ensure_ascii=False, sort_keys=True)
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--input", dest="inputs", action="append", type=Path)
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args(argv)
    try:
        bundle = materialize(
            campaign=args.campaign,
            registry=args.registry,
            binding=args.binding,
            inputs=args.inputs or [],
            store=args.store,
            output=args.output,
            as_of=args.as_of,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Nations League LIVE materialization blocked: {exc}", file=sys.stderr)
        return 2
    print(
        f"Nations League LIVE materialized: {bundle['fixture_count']} fixtures, "
        f"digest {bundle['public_digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
