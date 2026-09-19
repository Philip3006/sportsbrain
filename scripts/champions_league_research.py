"""Build offline Champions League research-readiness evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Make the repository root importable when this file is invoked by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.football.champions_league_research import (
    build_candidate_artifact_manifest,
    build_readiness_report,
)


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _rows(path: Path | None, key: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = _load_json(path)
    if isinstance(payload, dict):
        payload = payload.get(key, [])
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise SystemExit(f"{path} must contain a JSON list of objects or a {key!r} field")
    return payload


def _write_or_print(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None:
        print(text, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="hash allow-listed source artifacts")
    manifest.add_argument("--root", type=Path, default=Path("."))
    manifest.add_argument("--output", type=Path)

    report = subparsers.add_parser("report", help="score explicit offline evidence")
    report.add_argument("--root", type=Path, default=Path("."))
    report.add_argument("--matches", type=Path)
    report.add_argument("--predictions", type=Path)
    report.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "manifest":
        _write_or_print(build_candidate_artifact_manifest(args.root), args.output)
        return 0

    payload = build_readiness_report(
        _rows(args.matches, "matches"),
        _rows(args.predictions, "predictions"),
        root=args.root,
    )
    _write_or_print(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
