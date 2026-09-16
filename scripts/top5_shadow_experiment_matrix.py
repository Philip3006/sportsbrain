"""Build a neutral Top-5 shadow experiment matrix without side effects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.football.top5_real_shadow_audit import load_json
from src.football.top5_shadow_experiment_matrix import (
    MatrixError,
    build_experiment_matrix,
    render_markdown,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default="both",
        help="render machine-readable JSON, concise Markdown, or both",
    )
    directory = parser.add_subparsers(dest="command", required=True).add_parser(
        "directory"
    )
    directory.add_argument("path", type=Path)
    directory.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default=argparse.SUPPRESS,
    )
    return parser


def _render(report: dict[str, object], output_format: str) -> None:
    if output_format in {"json", "both"}:
        print(json.dumps(report, indent=2, sort_keys=True))
    if output_format in {"markdown", "both"}:
        if output_format == "both":
            print("\n---\n")
        print(render_markdown(report))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payloads = [load_json(path) for path in sorted(args.path.rglob("*.json"))]
        _render(build_experiment_matrix(payloads), args.format)
        return 0
    except (MatrixError, OSError, ValueError) as exc:
        print(f"MATRIX_FAILED_CLOSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
