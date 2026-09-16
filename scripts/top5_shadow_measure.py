"""Measure audited Top-5 real-shadow evidence locally without side effects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.football.top5_real_shadow_audit import load_json
from src.football.top5_real_shadow_measurement import (
    MeasurementError,
    measure_directory_payloads,
    measure_session_payload,
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
    subparsers = parser.add_subparsers(dest="command", required=True)
    session = subparsers.add_parser("session")
    session.add_argument("artifact", type=Path)
    session.add_argument("--evidence", type=Path)
    session.add_argument(
        "--format", choices=("json", "markdown", "both"), default=argparse.SUPPRESS
    )
    directory = subparsers.add_parser("directory")
    directory.add_argument("path", type=Path)
    directory.add_argument(
        "--format", choices=("json", "markdown", "both"), default=argparse.SUPPRESS
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
        if args.command == "session":
            payload = load_json(args.artifact)
            evidence = load_json(args.evidence) if args.evidence else None
            report = measure_session_payload(payload, evidence_bundle=evidence)
        else:
            payloads = [load_json(path) for path in sorted(args.path.rglob("*.json"))]
            report = measure_directory_payloads(payloads)
        report = {
            key: value for key, value in report.items() if not key.startswith("_")
        }
        _render(report, args.format)
        return 0
    except (MeasurementError, OSError, ValueError) as exc:
        print(f"MEASUREMENT_FAILED_CLOSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
