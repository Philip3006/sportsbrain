"""Prepare, inspect, or validate a future Top-5 controlled shadow plan.

Every command is preparation-only.  This CLI never imports a provider
adapter, opens a transport, checks credentials remotely, or executes a run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.football.provider_cascade.preparation import (
    PreparationContractError,
    load_preparation,
    preparation_from_input_payload,
    write_preparation,
)

_BANNER = (
    "PREPARATION ONLY",
    "NO NETWORK",
    "NO BET",
    "NO PUBLICATION",
    "NO PRODUCTION ACTIVATION",
    "CEO AUTHORIZATION REQUIRED FOR REAL EXECUTION",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare", help="build a no-network preparation artifact"
    )
    prepare.add_argument("fixture_manifest", type=Path)
    prepare.add_argument(
        "--output", type=Path, help="absolute external/test output path"
    )

    inspect = commands.add_parser(
        "inspect", help="print a validated preparation artifact"
    )
    inspect.add_argument("preparation", type=Path)

    validate = commands.add_parser("validate", help="validate a preparation artifact")
    validate.add_argument("preparation", type=Path)
    return parser


def _read_mapping(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PreparationContractError("input JSON is unreadable") from exc
    if not isinstance(payload, Mapping):
        raise PreparationContractError("input JSON must be an object")
    return payload


def _print_banner() -> None:
    print("\n".join(_BANNER), file=sys.stderr)


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _print_banner()
    try:
        if args.command == "prepare":
            preparation = preparation_from_input_payload(
                _read_mapping(args.fixture_manifest)
            )
            stored = None
            if args.output is not None:
                stored = str(write_preparation(preparation, args.output))
            payload = preparation.as_payload()
            if stored is not None:
                payload["stored_path"] = stored
            _print_json(payload)
            return 0
        preparation = load_preparation(args.preparation)
        if args.command == "inspect":
            _print_json(preparation.as_payload())
        else:
            _print_json(
                {
                    "valid": True,
                    "preparation_id": preparation.preparation_id,
                    "preparation_status": preparation.preparation_status,
                    "preparation_digest": preparation.preparation_digest,
                }
            )
        return 0
    except (PreparationContractError, OSError, TypeError, ValueError) as exc:
        print(f"FAIL CLOSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
