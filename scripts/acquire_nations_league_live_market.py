"""Acquire due current Nations League 1X2 snapshots for the LIVE seam."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_live_runtime import (
    NationsLeagueLiveRuntimeError,
    load_live_store,
)
from src.scanner.nations_league_live_market import (
    NationsLeagueLiveMarketError,
    acquire_live_market_snapshots,
    prepare_market_preflight,
    write_market_snapshot_batch,
)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NationsLeagueLiveMarketError("future fixture manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise NationsLeagueLiveMarketError("future fixture manifest must be an object")
    return value


def _campaign_records(path: Path) -> list[dict]:
    value = _read(path)
    records = value.get("records")
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise NationsLeagueLiveMarketError("forward campaign records are invalid")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument(
        "--campaign",
        type=Path,
        help="immutable forward-campaign records used for phase idempotency",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="perform only the provider-free due/idempotency decision",
    )
    args = parser.parse_args(argv)
    try:
        manifest = _read(args.manifest)
        existing_records = load_live_store(args.store)
        if args.campaign is not None:
            existing_records.extend(_campaign_records(args.campaign))
        if args.preflight:
            batch = prepare_market_preflight(
                manifest,
                as_of=args.as_of,
                existing_records=existing_records,
            )
        else:
            batch = acquire_live_market_snapshots(
                manifest,
                as_of=args.as_of,
                existing_records=existing_records,
            )
        write_market_snapshot_batch(args.output, batch)
    except (OSError, TypeError, NationsLeagueLiveMarketError, NationsLeagueLiveRuntimeError) as exc:
        print(f"Nations League LIVE market capture blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(batch, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
