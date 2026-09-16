"""Create or validate a durable Top-5 real-shadow session from normalized input."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.football.top5_real_shadow_contracts import (
    NormalizedProviderObservation,
    RealShadowExperiment,
)
from src.football.top5_real_shadow_session import build_session_from_payload
from src.football.top5_real_shadow_session_evidence import build_shadow_evidence
from src.football.top5_real_shadow_session_storage import RealShadowSessionStore
from src.utils.atomic_io import atomic_write_json

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--observations", type=Path)
    source.add_argument(
        "--b2-intake-dir",
        type=Path,
        help="canonical Builder-2 intake directory containing manifest.json and receipt.json",
    )
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--created-at", required=True, help="Timezone-aware ISO-8601 timestamp")
    parser.add_argument("--min-lead-minutes", type=int, required=True)
    parser.add_argument("--max-lead-minutes", type=int, required=True)
    parser.add_argument("--max-odds-age-seconds", type=int, required=True)
    parser.add_argument("--kickoff-tolerance-seconds", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--evidence-output",
        type=Path,
        help="optional external path for the top5-shadow-evidence-v1 bundle",
    )
    parser.add_argument("--fixture-mode", action="store_true", help="Use TEST_FIXTURE inputs; requires an explicit output path")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise TypeError(f"{path.name} must contain a JSON object")
    return payload


def _external_path(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("evidence output must be an absolute external path")
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("evidence output cannot be inside the active checkout")
    if "ledger" in str(resolved).lower() or "offline_replay" in str(resolved):
        raise ValueError("evidence output cannot use a ledger or replay path")
    return resolved


def _observations_payload(args: argparse.Namespace) -> dict[str, object]:
    if args.b2_intake_dir is None:
        payload = _read_json(args.observations)
        return payload
    if args.fixture_mode:
        raise ValueError("Builder-2 intake cannot be combined with fixture mode")
    intake_dir = args.b2_intake_dir.expanduser()
    manifest = _read_json(intake_dir / "manifest.json")
    receipt = _read_json(intake_dir / "receipt.json")
    observation = NormalizedProviderObservation.from_builder2_package(
        manifest.get("observation"), receipt
    )
    return {
        "league_scope": [observation.league_code],
        "observations": [observation.as_payload()],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run and args.evidence_output is not None:
        raise SystemExit("--evidence-output cannot be used with --dry-run")
    payload = _observations_payload(args)
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
    evidence_output = None
    if args.evidence_output is not None:
        evidence_output = _external_path(args.evidence_output)
        atomic_write_json(evidence_output, build_shadow_evidence(session))
    report = {
        "manifest": session.manifest(),
        "stored": str(output) if output else None,
        "evidence": str(evidence_output) if evidence_output else None,
    }
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
