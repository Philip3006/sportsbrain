#!/usr/bin/env python3
"""Build the versioned, offline Nations League causal-state dataset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.nations_league_competition_state import (
    DEFAULT_CONTRACTS,
    DEFAULT_COVERAGE,
    DEFAULT_DATASET,
    DEFAULT_SOURCE,
    DEFAULT_TIMELINE,
    _read_json,
    build_dataset,
    validate_dataset,
)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def build(
    source_path: Path,
    contract_path: Path,
    dataset_path: Path,
    coverage_path: Path,
    built_at: str | None = None,
    timeline_path: Path = DEFAULT_TIMELINE,
) -> tuple[dict, dict]:
    source, contracts, timeline = (
        _read_json(source_path),
        _read_json(contract_path),
        _read_json(timeline_path),
    )
    fixed_time = built_at or source["snapshot_committed_at"]
    dataset, coverage = build_dataset(
        source, contracts, built_at=fixed_time, timeline=timeline
    )
    validate_dataset(
        dataset, coverage, expected_source_digest=source["snapshot_digest"]
    )
    _atomic_json(dataset_path, dataset)
    _atomic_json(coverage_path, coverage)
    return dataset, coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE)
    parser.add_argument("--timeline", type=Path, default=DEFAULT_TIMELINE)
    parser.add_argument(
        "--built-at",
        help="Fixed UTC build timestamp; defaults to source snapshot commit time",
    )
    args = parser.parse_args()
    dataset, coverage = build(
        args.source,
        args.contracts,
        args.dataset,
        args.coverage,
        args.built_at,
        args.timeline,
    )
    print(
        json.dumps(
            {
                "schema_version": dataset["schema_version"],
                "records": len(dataset["records"]),
                "dataset_digest": coverage["dataset_digest"],
                "coverage_digest": coverage["coverage_digest"],
                "fixture_coverage_complete": coverage["fixture_coverage_complete"],
                "status": coverage["status"],
                "kickoff_timestamps_present": coverage["fields"]["kickoff_timestamp"][
                    "present"
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
