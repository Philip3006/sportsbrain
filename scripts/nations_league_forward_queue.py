"""Plan or manually execute the offline Nations League forward queue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_forward_queue import (
    DEFAULT_SHADOW_STORE,
    QueueError,
    build_capture_plan,
    execute_plan,
    read_shadow_store,
    render_summary,
)


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--as-of", required=True, help="explicit UTC timestamp")
    parser.add_argument("--store", type=Path, default=Path(DEFAULT_SHADOW_STORE))
    parser.add_argument("--plan", action="store_true", help="inspect only (default)")
    parser.add_argument(
        "--execute", action="store_true", help="execute locally and append JSONL"
    )
    parser.add_argument("--input-state", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.plan and args.execute:
            raise QueueError("--plan and --execute are mutually exclusive")
        manifest = _read_json(args.manifest)
        existing = read_shadow_store(args.store)
        plan = build_capture_plan(
            manifest,
            as_of=args.as_of,
            destination_shadow_store=str(args.store),
            existing_records=existing,
        )
        print(render_summary(plan))
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
        if not args.execute:
            return 0
        if args.input_state is None:
            raise QueueError("--execute requires --input-state")
        results = execute_plan(
            manifest,
            plan,
            input_state=_read_json(args.input_state),
        )
        print(json.dumps({"execution": results}, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError, QueueError) as exc:
        print(f"forward lifecycle queue blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
