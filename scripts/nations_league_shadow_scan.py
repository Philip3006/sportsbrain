"""Run one read-only, non-betting UEFA Nations League shadow scan."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.scanner.nations_league_shadow import (
    NationsLeagueShadowError,
    run_shadow_scan,
)


def main() -> int:
    try:
        artifact, path = run_shadow_scan()
    except NationsLeagueShadowError as exc:
        print(f"Nations League shadow scan blocked: {exc}", file=sys.stderr)
        return 2
    print(
        f"Shadow artifact: {path} "
        f"(fixtures={artifact['fixture_count']}, covered={artifact['covered_fixture_count']}, "
        f"skipped={len(artifact['skipped_fixtures'])}, digest={artifact['artifact_digest']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
