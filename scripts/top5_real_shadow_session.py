"""Create or validate a durable Top-5 real-shadow session from normalized input."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.football.top5_real_shadow_contracts import RealShadowExperiment
from src.football.top5_real_shadow_session import build_session_from_payload
from src.football.top5_real_shadow_session_storage import RealShadowSessionStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--created-at", required=True, help="Timezone-aware ISO-8601 timestamp")
    parser.add_argument("--min-lead-minutes", type=int, required=True)
    parser.add_argument("--max-lead-minutes", type=int, required=True)
    parser.add_argument("--max-odds-age-seconds", type=int, required=True)
    parser.add_argument("--kickoff-tolerance-seconds", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture-mode", action="store_true", help="Use TEST_FIXTURE inputs; requires an explicit output path")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    payload = json.loads(args.observations.read_text())
    if not isinstance(payload, dict):
        raise SystemExit("observations input must be a JSON object")
    experiment = RealShadowExperiment(
        args.experiment_id,
        args.min_lead_minutes,
        args.max_lead_minutes,
        args.max_odds_age_seconds,
        args.kickoff_tolerance_seconds,
    )
    created_at = datetime.fromisoformat(args.created_at.replace("Z", "+00:00"))
    session = build_session_from_payload(
        payload,
        experiment=experiment,
        session_key=args.session_key,
        integration_sha=args.integration_sha,
        created_at=created_at,
        fixture_mode=args.fixture_mode,
    )
    output = None
    if not args.dry_run:
        output = RealShadowSessionStore(args.output).save(session)
    report = {"manifest": session.manifest(), "stored": str(output) if output else None}
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
