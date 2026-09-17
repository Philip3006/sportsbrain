"""Evaluate a redacted APP-B2 TheRundown evidence envelope offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.football.top5_therundown_qualification import (
    TheRundownQualificationError,
    TheRundownQualificationStatus,
    evaluate_therundown_qualification,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--maximum-odds-age-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.evidence.read_text(encoding="utf-8"))
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
