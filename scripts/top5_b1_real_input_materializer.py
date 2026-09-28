#!/usr/bin/env python3
"""Materialize private, non-authorizing B1 inputs from a real B4 dossier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.football.top5_b1_real_input_materializer import (
    Top5B1MaterializationError,
    materialize_top5_b1_real_inputs,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build private B1 lifecycle/prepublication evidence from a previously "
            "validated real iSports B4 dossier. Makes no provider requests and "
            "does not activate or publish."
        )
    )
    parser.add_argument("--b4-dossier", required=True, type=Path)
    parser.add_argument("--publisher-workspace", required=True, type=Path)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="new absolute private output directory outside the checkout/publisher",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = materialize_top5_b1_real_inputs(
            dossier_path=args.b4_dossier,
            publisher_workspace=args.publisher_workspace,
            output_directory=args.output_dir,
        )
    except (Top5B1MaterializationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "TOP5_B1_REAL_INPUT_MATERIALIZATION_BLOCKED",
                    "reason": str(exc)[:256],
                    "provider_requests": 0,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
