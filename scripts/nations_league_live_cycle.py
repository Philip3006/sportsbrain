"""Run one deterministic Nations League LIVE lifecycle cycle.

The command is plan-only by default.  ``--execute-offline`` may materialize
provider-free predictions from a sealed #229 input-state, but it never calls a
provider, reads credentials, publishes, bets, or touches the ledger.
"""

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
    load_active_release,
    run_live_cycle,
)
from src.utils.atomic_io import atomic_write_text


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _read_store(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError("live store row is not an object")
            rows.append(value)
    return rows


def run_cycle(
    *,
    manifest: Path,
    input_state: Path,
    registry: Path,
    as_of: str,
    store: Path,
    execute_offline: bool,
) -> dict:
    active = load_active_release(registry)
    result = run_live_cycle(
        _read(manifest),
        _read(input_state),
        active,
        as_of=as_of,
        existing_records=_read_store(store),
        execute=execute_offline,
    )
    if execute_offline and result["appended_records"]:
        existing = _read_store(store)
        rows = existing + result["appended_records"]
        atomic_write_text(
            store,
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        )
        result = {**result, "store_status": "APPENDED"}
    else:
        result = {**result, "store_status": "PLAN_ONLY"}
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input-state", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--execute-offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = run_cycle(
            manifest=args.manifest,
            input_state=args.input_state,
            registry=args.registry,
            as_of=args.as_of,
            store=args.store,
            execute_offline=args.execute_offline,
        )
    except (OSError, ValueError, KeyError, TypeError, NationsLeagueLiveRuntimeError) as exc:
        print(f"Nations League LIVE cycle blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
