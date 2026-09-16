"""Audit Top-5 real-shadow evidence locally without network or side effects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.football.top5_real_shadow_audit import (
    ShadowAuditError,
    audit_directory_payloads,
    audit_prediction_payload,
    audit_session_payload,
    load_json,
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
    for name in ("prediction", "session"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("artifact", type=Path)
        subparser.add_argument("--evidence", type=Path)
        subparser.add_argument("--sample-report", type=Path)
        subparser.add_argument(
            "--format",
            choices=("json", "markdown", "both"),
            default=argparse.SUPPRESS,
        )
    directory = subparsers.add_parser("directory")
    directory.add_argument("path", type=Path)
    directory.add_argument(
        "--format",
        choices=("json", "markdown", "both"),
        default=argparse.SUPPRESS,
    )
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("audit", type=Path)
    inspect.add_argument(
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
        if args.command == "inspect":
            report = dict(load_json(args.audit))
        elif args.command == "prediction":
            report = audit_prediction_payload(load_json(args.artifact))
        elif args.command == "session":
            payload = load_json(args.artifact)
            evidence = load_json(args.evidence) if args.evidence else None
            sample = load_json(args.sample_report) if args.sample_report else None
            report = audit_session_payload(
                payload,
                evidence_bundle=evidence,
                sample_report=sample,
            )
        else:
            paths = sorted(args.path.rglob("*.json"))
            report = audit_directory_payloads([load_json(path) for path in paths])
        _render(report, args.format)
        return 0
    except ShadowAuditError as exc:
        print(f"AUDIT_FAILED_CLOSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
