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
    load_fixture_timeline,
    run_final_timeline_replay,
    run_replay,
    write_artifacts,
    write_final_timeline_artifacts,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-cache",
        type=Path,
        required=False,
        help="Existing local results pickle; never fetched/refreshed",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("results/audits"))
    parser.add_argument(
        "--timeline",
        type=Path,
        help="Canonical PR #215 timeline JSON for the final strict replay",
    )
    parser.add_argument(
        "--provisional-audit",
        type=Path,
        default=Path("results/audits/nations_league_causal_gbt_replay_20260929.json"),
        help="Existing provisional PR #214 audit used only for comparison",
    )
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--max-dc-iter", type=int, default=2000)
    parser.add_argument(
        "--pr214-input-head",
        default="48b14907f89bd97cd920546df6d3cfa5c4bbc3eb",
        help="Reviewed PR #214 input head bound into the final audit",
    )
    args = parser.parse_args()
    if args.bootstrap_replicates < 100:
        parser.error("--bootstrap-replicates must be >= 100 for reportable intervals")
    if args.timeline:
        timeline, timeline_digest = load_fixture_timeline(args.timeline)
        provisional = json.loads(args.provisional_audit.read_text(encoding="utf-8"))
        interim_path = Path("results/audits/nations_league_causal_gbt_subset_replay_20260929.json")
        interim = json.loads(interim_path.read_text(encoding="utf-8")) if interim_path.exists() else None
        audit, predictions = run_final_timeline_replay(
            timeline,
            timeline_digest,
            bootstrap_replicates=args.bootstrap_replicates,
            max_dc_iter=args.max_dc_iter,
            provisional_audit=provisional,
            interim_audit=interim,
            pr214_input_head=args.pr214_input_head,
        )
        paths = write_final_timeline_artifacts(audit, predictions, args.output_dir)
    else:
        if not args.results_cache:
            parser.error("--results-cache is required when --timeline is omitted")
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
                "evaluated_fixture_count": audit.get(
                    "evaluated_fixture_count", audit.get("common_fixture_count")
                ),
                "artifacts": [str(path) for path in paths],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
