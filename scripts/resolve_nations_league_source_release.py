"""Resolve the governed source release marker for the offline NL LIVE cycle."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def resolve_source_release_sha(marker_path: Path, *, repository_root: Path) -> str:
    """Return the marker's committed, successful, ancestor source release."""

    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("source provenance marker is unavailable") from exc
    if not isinstance(marker, dict) or marker.get("schema_version") != "1":
        raise ValueError("source provenance marker schema is invalid")
    source_sha = marker.get("source_release_sha")
    source_ci = marker.get("source_ci")
    if not SHA40.fullmatch(str(source_sha or "")) or not isinstance(source_ci, dict):
        raise ValueError("source provenance marker is malformed")
    if (
        source_ci.get("status") != "success"
        or source_ci.get("head_sha") != source_sha
        or not isinstance(source_ci.get("workflow"), str)
        or not source_ci["workflow"].strip()
    ):
        raise ValueError("source provenance CI marker is invalid")
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"],
            cwd=repository_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            text=True,
        ).strip()
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", source_sha, head_sha],
            cwd=repository_root,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("source release is not a committed ancestor") from exc
    return source_sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(resolve_source_release_sha(args.marker, repository_root=Path.cwd()))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
