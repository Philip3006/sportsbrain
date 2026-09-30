#!/usr/bin/env python3
"""Build and validate the offline UEFA Nations League fixture timeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.nations_league_fixture_timeline import (
    DEFAULT_CONTRACTS,
    DEFAULT_COVERAGE,
    DEFAULT_RESULTS,
    DEFAULT_SCHEDULE,
    DEFAULT_TIMELINE,
    _read,
    build_timeline,
    validate_timeline,
)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build(
    results_path: Path = DEFAULT_RESULTS,
    contracts_path: Path = DEFAULT_CONTRACTS,
    schedule_path: Path = DEFAULT_SCHEDULE,
    timeline_path: Path = DEFAULT_TIMELINE,
    coverage_path: Path = DEFAULT_COVERAGE,
) -> tuple[dict, dict]:
    timeline, coverage = build_timeline(
        _read(results_path), _read(contracts_path), _read(schedule_path)
    )
    validate_timeline(timeline, coverage)
    _atomic_json(timeline_path, timeline)
    _atomic_json(coverage_path, coverage)
    return timeline, coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument("--timeline", type=Path, default=DEFAULT_TIMELINE)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    args = parser.parse_args()
    timeline, coverage = build(
        args.results,
        args.contracts,
        args.schedule,
        args.timeline,
        args.coverage,
    )
    print(
        json.dumps(
            {
                "status": coverage["status"],
                "records": len(timeline["records"]),
                "verified_utc_kickoffs": coverage["verified_utc_kickoffs"],
                "result_safe_bounds": coverage["result_safe_bounds"],
                "unresolved": coverage["unresolved_count"],
                "dataset_digest": coverage["dataset_digest"],
                "coverage_digest": coverage["coverage_digest"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
