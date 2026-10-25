"""Run the offline causal international-results backtest on a local source CSV."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from src.analysis.international_large_backtest import (
    load_canonical_csv,
    render_markdown_report,
    run_backtest,
    write_json_atomic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-csv",
        type=Path,
        required=True,
        help="Already-downloaded canonical results.csv",
    )
    parser.add_argument(
        "--main-sha", required=True, help="Exact origin/main SHA used as analysis base"
    )
    parser.add_argument(
        "--source-fetched-at",
        help="UTC fetch time; defaults to the local file modification time",
    )
    parser.add_argument(
        "--odds-path", type=Path, default=Path("data/odds_history.json")
    )
    parser.add_argument(
        "--nl-validation",
        type=Path,
        default=Path("results/audits/nations_league_model_validation_20260927.json"),
    )
    parser.add_argument(
        "--nl-shadow",
        type=Path,
        default=Path("results/audits/nations_league_shadow_signals_20260928.json"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("results/audits/international_large_backtest_20260928.json"),
    )
    parser.add_argument(
        "--output-report",
        type=Path,
        default=Path("docs/international_large_backtest_20260928.md"),
    )
    parser.add_argument("--dc-max-iter", type=int, default=2000)
    parser.add_argument("--bootstrap-replicates", type=int, default=1000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    source = args.source_csv.resolve()
    results, raw_rows, source_sha = load_canonical_csv(source)
    fetched_at = (
        args.source_fetched_at
        or datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc).isoformat()
    )
    payload = run_backtest(
        results,
        source_row_count=raw_rows,
        source_sha256=source_sha,
        source_fetched_at=fetched_at,
        odds_path=args.odds_path,
        nl_validation_path=args.nl_validation,
        nl_shadow_path=args.nl_shadow,
        main_sha=args.main_sha,
        generated_at=datetime.now(timezone.utc).isoformat(),
        dc_max_iter=args.dc_max_iter,
        bootstrap_replicates=args.bootstrap_replicates,
    )
    write_json_atomic(payload, args.output_json)
    report_path = args.output_report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(render_markdown_report(payload), encoding="utf-8")
    temporary.replace(report_path)
    print(f"source_rows={raw_rows}")
    print(f"source_sha256={source_sha}")
    print(f"analysis_sha={args.main_sha}")
    print(f"json={args.output_json}")
    print(f"report={args.output_report}")
    print(f"no_lookahead_verified={payload['no_lookahead_audit']['verified']}")
    print(f"provider_requests={payload['safety']['provider_requests']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
