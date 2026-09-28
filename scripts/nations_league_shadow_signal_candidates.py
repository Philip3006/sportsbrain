"""Materialize candidates from the exact frozen Nations League public bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.nations_league_shadow_signals import build_shadow_candidate_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        default="tests/fixtures/nations_league_public_incident_20260928.json",
        help="canonical frozen public bundle (must match the pinned incident digest)",
    )
    parser.add_argument(
        "--output",
        default="results/audits/nations_league_shadow_signals_20260928.json",
        help="candidate artifact output path",
    )
    parser.add_argument(
        "--generated-at",
        help="optional timezone-aware UTC timestamp for reproducible artifact metadata",
    )
    args = parser.parse_args()

    bundle = json.loads(Path(args.input).read_text(encoding="utf-8"))
    generated_at = (
        datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
        if args.generated_at
        else datetime.now(timezone.utc)
    )
    artifact = build_shadow_candidate_artifact(bundle, generated_at=generated_at)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "artifact": str(output),
                "artifact_digest": artifact["artifact_digest"],
                **artifact["summary"],
                "safety": artifact["safety"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
