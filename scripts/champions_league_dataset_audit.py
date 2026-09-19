"""Emit a deterministic local Champions League dataset-readiness manifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.football.champions_league_dataset_audit import (  # noqa: E402
    DatasetAuditError,
    build_manifest,
)


def _write(payload: dict[str, Any], output: Path | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None:
        print(text, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("manifest", "audit the default local CL dataset directory"),
        ("audit", "audit explicitly supplied local CL dataset files"),
    ):
        subparser = subparsers.add_parser(name, help=help_text)
        subparser.add_argument("--root", type=Path, default=ROOT)
        subparser.add_argument("--dataset", type=Path, action="append", default=[])
        subparser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    dataset_paths = args.dataset or None
    if dataset_paths:
        dataset_paths = [path if path.is_absolute() else root / path for path in dataset_paths]
    try:
        payload = build_manifest(root, dataset_paths)
    except DatasetAuditError as exc:
        raise SystemExit(str(exc)) from exc
    output = args.output
    if output is not None and not output.is_absolute():
        output = root / output
    _write(payload, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
