"""Run the offline Nations League causal-envelope GBT research replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.nations_league_causal_gbt_replay import (
    file_sha256,
    run_replay,
    write_artifacts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-cache",
        type=Path,
        required=True,
        help="Existing local results pickle; never fetched/refreshed",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("results/audits"))
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--max-dc-iter", type=int, default=2000)
    args = parser.parse_args()
    if args.bootstrap_replicates < 100:
        parser.error("--bootstrap-replicates must be >= 100 for reportable intervals")
    results = pd.read_pickle(args.results_cache)
    audit, predictions = run_replay(
        results,
        file_sha256(args.results_cache),
        bootstrap_replicates=args.bootstrap_replicates,
        max_dc_iter=args.max_dc_iter,
    )
    paths = write_artifacts(audit, predictions, args.output_dir)
    print(
        json.dumps(
            {
                "research_status": audit["research_status"],
                "target_fixture_count": audit["target_fixture_count"],
                "common_fixture_count": audit["common_fixture_count"],
                "artifacts": [str(path) for path in paths],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
