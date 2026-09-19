"""Evaluate a redacted APP-B2 TheRundown evidence envelope offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.football.top5_therundown_qualification import (
    PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION,
    TheRundownQualificationError,
    TheRundownQualificationStatus,
    evaluate_therundown_pr88_evidence_summary,
    evaluate_therundown_qualification,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--maximum-odds-age-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
        if payload.get("schema_version") == PR88_REAL_EVIDENCE_SUMMARY_SCHEMA_VERSION:
            report = evaluate_therundown_pr88_evidence_summary(payload)
        else:
            report = evaluate_therundown_qualification(
                payload, maximum_odds_age_seconds=args.maximum_odds_age_seconds
            )
    except (OSError, json.JSONDecodeError, TheRundownQualificationError) as exc:
        print(f"QUALIFICATION_FAILED_CLOSED: {exc}")
        return 2
    print(json.dumps(report.as_payload(), indent=2, sort_keys=True))
    return (
        0
        if report.status is TheRundownQualificationStatus.QUALIFIED_EVIDENCE_READY
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
