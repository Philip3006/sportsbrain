"""Preserve only the freshest valid public Nations League projection."""

# Imports follow repository-root insertion so direct script execution works.
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_USER
from src.notifications.nations_league_public import (
    select_freshest_valid_public_nations_league,
)
from src.utils.atomic_io import atomic_write_json, atomic_write_text


def _read_snapshot(path: Path, *, required: bool) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise ValueError(f"cannot read JSON snapshot: {path}") from exc
        return None
    if not isinstance(value, Mapping):
        if required:
            raise ValueError(f"snapshot is not a JSON object: {path}")
        return None
    return dict(value)


def _signal_siblings(directory: Path, target_name: str) -> list[Path]:
    return [
        directory / target_name,
        directory / f"signals_{DEFAULT_USER}.json",
        directory / "signals.json",
    ]


def merge_public_projection(*, target: Path, base: Path, candidates: list[Path]) -> str:
    """Write base signal data while retaining the freshest valid NL public value."""
    try:
        base_text = base.read_text(encoding="utf-8")
        parsed_base = json.loads(base_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON snapshot: {base}") from exc
    if not isinstance(parsed_base, Mapping):
        raise TypeError(f"snapshot is not a JSON object: {base}")
    base_snapshot = dict(parsed_base)

    ordered_paths = [target]
    ordered_paths.extend(_signal_siblings(target.parent, target.name))
    ordered_paths.extend(candidates)
    ordered_paths.append(base)
    ordered_paths.extend(_signal_siblings(base.parent, base.name))

    seen: set[Path] = set()
    public_candidates: list[object] = []
    for path in ordered_paths:
        resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        snapshot = _read_snapshot(path, required=False)
        if snapshot is not None and "nations_league" in snapshot:
            public_candidates.append(snapshot["nations_league"])

    selected = select_freshest_valid_public_nations_league(public_candidates)
    had_public_value = "nations_league" in base_snapshot
    public_value_unchanged = (selected is None and not had_public_value) or (
        selected is not None and selected == base_snapshot.get("nations_league")
    )
    if selected is None:
        base_snapshot.pop("nations_league", None)
        result = "omitted"
    else:
        base_snapshot["nations_league"] = selected
        result = "retained"

    if public_value_unchanged:
        if target.absolute() != base.absolute():
            atomic_write_text(target, base_text)
    else:
        atomic_write_json(target, base_snapshot, indent=2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--candidate-file", action="append", default=[], type=Path)
    args = parser.parse_args()
    try:
        result = merge_public_projection(
            target=args.target, base=args.base, candidates=args.candidate_file
        )
    except (TypeError, ValueError) as exc:
        print(f"Nations League public merge failed closed: {exc}", file=sys.stderr)
        return 1
    print(f"Nations League public projection {result}: {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
