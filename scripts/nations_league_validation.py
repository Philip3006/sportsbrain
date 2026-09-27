"""Run the offline UEFA Nations League 1X2 transferability audit."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis.nations_league_validation import (
    load_local_results,
    run_validation,
    write_json_atomic,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-cache",
        required=True,
        type=Path,
        help="Explicit local canonical international-results pickle; never fetched automatically.",
    )
    parser.add_argument(
        "--odds-history",
        type=Path,
        help="Optional local odds snapshot JSON; no provider request is made.",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        help="Optional frozen WM2026 snapshot directory for retrospective diagnostic only.",
    )
    parser.add_argument("--source-sha", required=True, help="Exact source/base commit being audited.")
    parser.add_argument(
        "--verified-current-main-sha",
        required=True,
        help="Freshly verified origin/main; may differ only in non-source data for local rehearsal.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/audits/nations_league_model_validation_20260927.json"),
    )
    parser.add_argument("--dc-max-iter", type=int, default=2000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    results, cache_digest = load_local_results(args.results_cache)
    report = run_validation(
        results=results,
        cache_sha256=cache_digest,
        odds_path=args.odds_history,
        snapshot_dir=args.snapshot_dir,
        source_sha=args.source_sha,
        verified_current_main_sha=args.verified_current_main_sha,
        max_iter=args.dc_max_iter,
    )
    write_json_atomic(report, args.output)
    print(
        f"Wrote {args.output}: matches={report['strict_validation']['match_count']}, "
        f"no_lookahead_verified={report['no_lookahead_verified']}, "
        f"recommended_shadow_status={report['recommended_shadow_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
