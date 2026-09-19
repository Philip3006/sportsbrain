#!/usr/bin/env python3
"""Read-only CLI for the Top-5 production verification contracts.

The input is evidence captured by a separately authorized operator.  This
command never contacts a provider and never writes runtime, scheduler,
publication, ledger, or Cloudflare state.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.football.top5_production_verification import (
    ActivationIdentity,
    MatchObservation,
    PreActivationBaseline,
    ResourceEvidence,
    RoutingEvidence,
    RuntimeEvidence,
    evaluate_publication_gate,
    rollback_decision,
    verification_report_from_mapping,
    verify_production,
)


def _load(path: str) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON evidence: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("evidence root must be a JSON object")
    return payload


def _dump(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _cmd_baseline(payload: dict[str, object]) -> int:
    baseline = PreActivationBaseline.from_mapping(payload.get("baseline") or payload)
    baseline.validate()
    _dump({"baseline_valid": True, "captured_at": baseline.captured_at.isoformat(), "activation": baseline.activation.as_payload()})
    return 0


def _cmd_verify(payload: dict[str, object]) -> int:
    baseline = PreActivationBaseline.from_mapping(payload["baseline"])
    observed_activation = ActivationIdentity.from_mapping(payload["observed_activation"])
    routing = RoutingEvidence.from_mapping(payload["routing"])
    observations = tuple(MatchObservation.from_mapping(item) for item in payload.get("observations", ()))
    runtime = RuntimeEvidence.from_mapping(payload["runtime"])
    resources = ResourceEvidence.from_mapping(payload["resources"])
    report = verify_production(
        baseline,
        observed_activation,
        routing,
        observations,
        runtime,
        resources,
        checked_at=datetime.fromisoformat(str(payload["checked_at"]).replace("Z", "+00:00")),
        max_observation_age_seconds=payload.get("max_observation_age_seconds", 900),
    )
    _dump(report.as_payload())
    return {"PRODUCTION_VERIFIED": 0, "ROLLBACK_REQUIRED": 3, "VERIFICATION_BLOCKED": 2}[report.status.value]


def _cmd_rollback(payload: dict[str, object]) -> int:
    report = verification_report_from_mapping(payload.get("report") or payload)
    _dump(rollback_decision(report))
    return 0


def _cmd_publication(payload: dict[str, object]) -> int:
    report = verification_report_from_mapping(payload["report"])
    current = ActivationIdentity.from_mapping(payload["current_activation"])
    result = evaluate_publication_gate(
        report,
        current,
        ceo_publication_authorization=payload.get("ceo_publication_authorization"),
    )
    _dump(result.as_payload())
    return 0 if result.eligible else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("baseline", "verify", "rollback", "publication-preflight"))
    parser.add_argument("--input", required=True, help="JSON evidence file")
    args = parser.parse_args(argv)
    try:
        payload = _load(args.input)
        if args.command == "baseline":
            return _cmd_baseline(payload)
        if args.command == "verify":
            return _cmd_verify(payload)
        if args.command == "rollback":
            return _cmd_rollback(payload)
        return _cmd_publication(payload)
    except (KeyError, TypeError, ValueError) as exc:
        _dump({"status": "VERIFICATION_BLOCKED", "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
